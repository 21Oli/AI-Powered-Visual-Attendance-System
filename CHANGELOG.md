# CHANGELOG.md

All notable changes to the AI-Powered Visual Attendance System.
Contains **actual implemented changes only** — future work is not listed
as done. Format based on Keep a Changelog.

## [0.11.0] — 2026-10-13 — Phase 1, Step 11: Temporal / multi-frame verification

### Added

* `src/temporal_verification.py`: `TemporalVerifier` — deterministic
  multi-frame temporal verification **on top of** the Step 10 result
  contract (consumes `RecognitionResult` values one at a time; no
  duplication of cosine similarity, thresholds, storage, or manifest
  logic). Evidence policy: only consecutive `MATCHED` frames for the
  **same** employee ID accumulate positive evidence; `UNKNOWN`,
  `AMBIGUOUS`, and `NO_CANDIDATES` never contribute and **break** the
  streak (`INTERRUPTED`, no identity reported); a matched frame for a
  different ID starts a fresh candidate streak (cross-ID evidence is
  never combined). Verification: `VERIFIED(employee_id)` exactly when
  the uninterrupted same-ID streak reaches `min_consistent_frames`
  (frozen `TemporalVerificationResult` with
  `COLLECTING`/`VERIFIED`/`INTERRUPTED` statuses, bounded
  `streak_frames`/`window_frames_used` counters, short reason).
  `reset()` clears all evidence (instance-scoped); one instance per
  tracked face/session — no shared state. Bounded retention only
  (deque capped at `window_frames`); no frames, crops, embeddings,
  scores, or names retained; employee IDs preserved verbatim
  (`"007"`, `"EMP-0042"`). Repeated post-verification frames are
  idempotent state reporting, not recurring verification events.
* `src/config.py`: frozen `TemporalVerificationConfig` +
  `TEMPORAL_VERIFICATION` — `min_consistent_frames=3`,
  `window_frames=10`, both **initial engineering defaults** pending
  Phase 2 real-camera calibration; strict validation (plain ints,
  bools/floats rejected; `window_frames >= min_consistent_frames`).
* `tests/test_temporal_verification.py`: 51 deterministic,
  hardware-free tests covering the temporal checklist (initial state,
  threshold, cross-ID isolation, ID preservation, per-status
  non-evidence, interruption, window bounds, reset & isolation,
  config validation, malformed `MATCHED` refusal, boundaries,
  bounded state, frozen results, and a side-effect-free
  import/construction subprocess check).

### Notes

* Temporal verification is **not** an attendance event; recording
  arrives in Step 13. Deterministic logic only — no real-camera or
  real-recognition-pipeline accuracy validation was performed.

## [0.10.0] — 2026-10-12 — Phase 1, Step 10: Face recognition

### Added

* `src/face_recognition.py`: `FaceRecognitionService` — one reusable,
  injectable recognition decision (crop → Step 08 embedder → Step 09
  storage candidates → deterministic cosine ranking → typed result).
  Decision only — no temporal verification (Step 11), no liveness, no
  unknown-face capture, no attendance events, no camera/writes/
  network. Recognition is a similarity-based decision, never proof of
  identity.
* Similarity & decision: pure cosine `a·b / (‖a‖‖b‖)` in [-1, 1]
  (`cosine_similarity`, unit-testable; validated 1-D/non-empty/
  finite/equal-dimension/non-zero-norm inputs; nothing mutated).
  Deterministic rank by `(-score, employee_id)`; statuses `MATCHED`/
  `UNKNOWN`/`AMBIGUOUS`/`NO_CANDIDATES` in frozen `RecognitionResult`
  (best/runner-up/margin scores + short reason; identity assigned
  only on unambiguous MATCHED). `best == threshold` ⇒ MATCHED;
  `margin == min_margin` ⇒ MATCHED; identical top scores ⇒ AMBIGUOUS
  (ties never resolve into identity). Single candidates decided on
  threshold alone.
* `src/config.py`: frozen `RecognitionConfig` + `RECOGNITION`
  — `min_similarity=0.36` (raw-cosine scale) and `min_margin=None`,
  both **explicitly provisional** until empirical calibration; range
  validation in `__post_init__`. The literature 0.6 threshold
  (normalized features) is deliberately not reused, because Step 08
  stores raw crop-only embeddings; `FaceProcessingConfig` unchanged.
* Employee eligibility via existing authorities only: the Step 07
  published enrollment manifest gates candidacy (no second source of
  truth); active status trusted from the manifest (or overridden by
  an injectable `active_lookup`); untrusted manifests are validated —
  unreadable/malformed/schema-mismatched/identity-contradicting ones
  raise the new typed `ManifestIntegrityError` (fail-closed default,
  documented `"skip"` escape hatch). Inactive employees are excluded
  from decisions but their records are never deleted. Stored IDs
  (`007`, `EMP-0042`) are preserved exactly.
* `src/enrollment.py`: backward-compatible public `safe_dir_name()`
  wrapper so the recognizer reuses the writer's exact encoded-name
  scheme (no duplication, no behavior change).
* Corruption & failure honesty: corrupt records surfaced via explicit
  candidates raise their typed Step 09 errors; a vanished record
  raises `RecognitionStorageError`; embedder failures raise
  `RecognitionModelError`; invalid inputs/config raise
  `RecognitionInputError`. Results/logs never contain crops or full
  vectors (privacy by contract, tested).
* `tests/test_face_recognition.py`: 74 deterministic tests on fake
  embedders + temp-dir real storage (no real SFace artifact, no
  camera, no network, no real biometrics) covering: matches, explicit
  subsets, unknown, exact threshold/margin boundaries, ambiguity,
  ties, candidate-set edges, vector validation matrix (NaN/inf/zero-
  norm/empty/dimension mismatch), corrupt-record propagation,
  embedder/storage failures, config validation, ID preservation,
  active/inactive + manifest integrity, model-compatibility honesty,
  immutability, privacy, import/construction/foreign-CWD/subprocess
  hygiene.

### Verified

* Focused recognition tests: 74 passed.
* `python -m pytest` → **495 passed** (421 prior + 74 new) from the
  project root and an external working directory; stable across
  repeated runs.
* `git diff --check` → clean; no biometric artifacts in the repo.

### Known limitations

* **Real SFace inference remains unverified** (artifact absent,
  never downloaded; Steps 08/09 status carried forward) — all
  recognition tests are fake-backed with real validation rigor, and
  no recognition accuracy claim is made.
* Model identity is not machine-enforced (no identifier exists in
  Step 09 sidecars and none was invented): all candidates in one
  deployment must come from one known, consistent embedding-model
  generation (documented operational remedy; no format change).
* Thresholds/margin are provisional starting points on the raw
  cosine scale; Phase 2 calibration required before production use.
* Not included per plan: temporal verification (Step 11), liveness,
  unknown-face capture, attendance events, camera loop.

## [0.9.0] — 2026-10-12 — Phase 1, Step 09: Embedding storage

### Added

* `src/embedding_storage.py`: `EmbeddingStorageService` — saves,
  loads, lists, and validates stored face embeddings under the
  existing configured `data/embeddings/` directory. Storage only: no
  matching (Step 10), no recognition, no attendance, no enrollment
  changes; storage proves nothing about identity.
* Storage format (one choice, documented): per employee one NumPy
  `.npz` array file (`np.savez` write / `np.load(allow_pickle=False)`
  read — no pickle, no object arrays) plus a JSON metadata sidecar
  (schema `format_version: 1`: exact employee ID, declared dimension,
  `float32` dtype marker, timezone-aware UTC `stored_at`). Record
  name stems reuse Step 07's traversal-proof percent-encoding; IDs
  (`007`, `EMP-0042`) survive byte-exactly and must satisfy the
  `Employee` model rules. No database, no new dependency.
* Contracts: `save(employee_id, embedding, *, replace=False)` accepts
  a Step 08 `FaceEmbedding` or a raw 1-D float array (normalized to
  the float32 on-disk contract); `load()` re-validates everything on
  every call treating stored files as untrusted input;
  `exists()` counts only complete record pairs;
  `list_employee_ids()` skips corrupt sidecars instead of crashing.
  The constructor's `expected_dimension` (default `0` = any positive
  dimension) is enforced on both save and load, making cross-model
  dimension compatibility explicit (`EmbeddingRecordFormatError`).
* Safety: save validates the vector (1-D, non-empty, floating, finite,
  non-zero norm) and the ID **before any write**; a record is never
  overwritten without the explicit `replace=True` flag (files staged,
  flushed, fsync-ed; hard-link publication fails loudly on a
  concurrent winner; publication order array-then-sidecar so a crash
  can leave an unpublished array but never a metadata-declared record
  without its validated sidecar); failed attempts clean only their
  own staged/unpublished artifacts. Typed errors:
  `EmbeddingStorageInputError` (ValueError),
  `DuplicateEmbeddingRecordError`, `EmbeddingRecordNotFoundError`,
  `CorruptEmbeddingRecordError`, `EmbeddingRecordFormatError`,
  `EmbeddingPersistenceError`. Returned records are frozen with
  write-protected vectors. Import/construction perform no filesystem
  work (directories created only when a real save needs them).
* `tests/test_embedding_storage.py`: 79 deterministic tests on
  synthetic vectors in temporary directories (no real biometrics, no
  network): round trips, exact-ID preservation, vector validation
  (NaN/±inf/zero-norm/shape/dtype), duplicate refusal + explicit
  replace, corruption matrix (garbage files, invalid JSON, object
  arrays, smuggled pickle bytes that must never execute, on-disk
  NaN, dtype/shape/dimension/ID mismatches, naive timestamps,
  unknown dtype markers, incompatible schema versions), traversal
  and malicious-ID rejection before any write, write-failure cleanup
  with unrelated data untouched, hygiene (import/construction/
  foreign-CWD/fresh-subprocess/no-device-no-network), and output
  isolation.

### Verified

* Focused embedding-storage tests: 79 passed.
* `python -m pytest` → **421 passed** (342 prior + 79 new) from the
  project root and an external working directory; stable across
  repeated runs.
* `git diff --check` → clean.
* No biometric artifacts in the repository (tests use temp dirs).

### Known limitations

* Storage does not consult enrollment manifests (deliberate — no
  second identity source-of-truth); recognizing only enrolled
  employees is Step 10's gating decision.
* Crash between the two file publications leaves an orphan array
  (never a declared record without metadata); orphans are skipped by
  `exists()`/`list_employee_ids()` but not garbage-collected.
* Model identity/version is not recorded (SFace exposes no version
  API); dimension compatibility is the explicit contract instead.
* Not included per plan: matching/recognition (Step 10), temporal
  verification, unknown-face handling, attendance logic.

## [0.8.0] — 2026-10-11 — Phase 1, Step 08: Face embedding generation

### Added

* `src/face_embedding.py`: `FaceEmbedder` — converts one BGR uint8
  face crop (the exact format Step 07 persists) into a validated
  float32 1-D embedding. Explicit `load()`/`is_loaded`/`embed()`
  lifecycle; import and construction are side-effect-free;
  `backend_factory` injectable (tests use fakes; the real default is
  `cv2.FaceRecognizerSF`, bundled in the pinned opencv-python wheel —
  no new dependency, no `onnxruntime`). Typed errors:
  `FaceEmbeddingInputError` (ValueError) / `FaceEmbeddingModelError`
  (RuntimeError) / `FaceEmbeddingOutputError` (RuntimeError).
  Validated frozen `FaceEmbedding` result: write-protected vector,
  dimension, pre-normalization L2 norm, explicit dtype marker.
* Output contract: 1-D, finite, dimension validated against the
  configured `expected_dimension` (0 = accept any non-empty),
  zero-norm rejected (never divided), L2 normalization only when
  `normalize_output=True` (contract-driven, not convention).
* `tests/test_face_embedding.py`: 33 deterministic tests on
  injected fakes — input contract, lifecycle, generation/output
  validation (NaN/inf/zero-norm/shape), output isolation,
  hygiene (import/construction/foreign-CWD/fresh-subprocess), and
  Step 07 PNG-crop format roundtrip compatibility.

### Model approach

* **SFace** (OpenCV Zoo `face_recognition_sface_2021dec.onnx`) chosen
  over insightface buffalo: buffalo's ArcFace path requires five
  landmarks that Step 07 crops do not carry; SFace's documented
  crop→112×112 preprocessing works on plain crops; Apache-2.0 (vs
  research-only buffalo); bundled DNN loader in the pinned wheel.
* The SFace artifact is **operator-supplied** at `models/sface.onnx`
  and **never downloaded**; until provided, `load()` raises
  `FaceEmbeddingModelError`. **Real-model inference is pending**
  (no artifact in this repository; unit tests are explicitly
  fake-backed).

### Verified

* Focused embedding tests: 33 passed.
* `python -m pytest` → **342 passed** (309 prior + 33 new) from the
  project root and an external working directory; stable across
  repeated runs.
* `git diff --check` → clean.

### Known limitations

* Crop-only (no landmark alignment) SFace accuracy is unmeasured;
  Phase 2 evaluation on real footage required before any accuracy
  claim.
* Not included per plan: embedding persistence (Step 09),
  matching/recognition (Step 10), liveness, attendance logic.

## [0.7.0] — 2026-10-11 — Phase 1, Step 07: Multi-image face enrollment

### Added

* `src/enrollment.py`: `EnrollmentService` — one controlled, bounded
  capture session per employee: identity validation via the existing
  `Employee` model (IDs preserved exactly, `007`/`EMP-0042`),
  duplicate-refusal before any capture, camera/detector/assessor
  orchestration through the existing Step 04–06 services, per-sample
  accept/reject with explicit typed reasons, and staged persistence.
  Typed errors: `EnrollmentInputError`, `DuplicateEnrollmentError`,
  `EnrollmentPersistenceError`. Results: frozen `EnrollmentResult` with
  COMPLETED/CANCELLED/FAILED status, accepted `SampleRecord`s,
  per-frame `SampleRejection`s (reason + measured quality), attempt
  count and duplicate-skip count. Camera released on every exit path;
  cancellation and failure never persist; persistence failures clean
  up only current-attempt artifacts. Camera, detector, assessor,
  config, and settings are injectable (hardware-free tests).
* `src/config.py`: `EnrollmentConfig.max_capture_attempts=60` — the
  bound preventing endless capture loops when frames keep failing;
  validated `>= min_samples`.
* Storage layout (both roots already git-ignored):
  `<data>/employees/<encoded-id>/employee.json` (schema version 1;
  full Employee serialization incl. `is_active`, sample count,
  per-sample file/UTC time/quality metrics) and
  `<data>/face_samples/<encoded-id>/sample_001.png`. `<encoded-id>` is
  a deterministic percent-encoding of the exact ID (dots encoded),
  making path traversal structurally impossible. Existing enrollments
  are never overwritten; samples stage in `.staging-` dirs, are
  verified by re-decoding, and publish samples-first,
  manifest-last.
* `tests/test_enrollment.py`: 38 deterministic tests with fakes and
  synthetic frames — validation, duplicate refusal, capture flow,
  cancellation, camera lifecycle/errors, service exceptions, attempt
  budget, byte-duplicate skipping, persistence layout/content,
  path-safety encoding, failure cleanup, and import hygiene.

### Verified

* Focused enrollment tests: 38 passed.
* `python -m pytest` → **309 passed** (271 prior + 38 new) from the
  project root and an external working directory; stable across
  repeated runs.
* Fresh-interpreter/foreign-CWD hygiene: import and service
  construction create no directories and touch no camera/model/files.
* `git diff --check` → clean.

### Known limitations

* Near-duplicate biometric similarity is out of scope (needs
  embeddings, Step 08); only exact byte-duplicate suppression exists.
* Multi-file persistence is best-effort atomic, not transactional: a
  crash between sample and manifest publication can leave orphan
  sample files — never a declared enrollment without images.
* Capture/sampling thresholds are initial defaults; Phase 2
  calibration pending.

### Not included (per plan — later steps)

* Embeddings (Step 08), recognition (Step 10), liveness, attendance
  logic, CLI/GUI enrollment UX.

## [0.6.0] — 2026-10-10 — Phase 1, Step 06: Face quality assessment

### Added

* `src/quality.py`: `FaceQualityAssessor` — deterministic per-face
  image-quality gating plus the frame face-count policy.
  * `assess_face(frame, detection)`: measured metrics (usable box size,
    Laplacian-variance sharpness, grayscale mean brightness) plus an
    optional landmark orientation proxy, returning a frozen `FaceQuality`
    with an explicit `FaceQualityDecision` (ACCEPT/REJECT) and a stable,
    machine-readable `FaceQualityReason` for the first-failed check
    (stable order: out_of_frame → too_small → no_landmarks/pose → blur
    → too_dark → too_bright). Rejections still carry the measured
    metrics; unusable inputs (fully-out-of-frame box) reject with all
    metrics `None` — nothing is invented.
  * `assess_frame_faces(detections)`: face-count policy kept separate
    from image quality — `no_faces` / `multiple_faces` / `None`. Even a
    policy-rejected multi-face frame leaves every face individually
    assessable for future multi-face use.
  * Errors mirror Step 05 conventions: `FaceQualityInputError`
    (a `ValueError`) for contract-violating inputs; typed reasons for
    valid inputs that fail quality gates. Frames and detections are
    never mutated; no camera/model/file/network surface.
* `src/config.py`: new frozen `QualityConfig` + project-wide `QUALITY`
  instance (Step 02 convention: tune thresholds in one place).
  Defaults are initial calibration starting points, not proven
  biometric cutoffs: `min_face_size_px=80`, `min_sharpness=50.0`,
  brightness window `[40, 220]`, `pose_yaw_tolerance=0.15`,
  `pose_pitch_tolerance=0.35`, `pose_policy="optional"|"required"`,
  `require_single_face=True`. Fail-fast `ValueError` validation.
* `src/face_detection.py` (backward-compatible): `FaceDetection` gains
  an optional `landmarks` field — five `(x, y)` points in canonical
  YuNet order, populated only from real 15-column backend rows, `None`
  otherwise; non-finite landmark values drop the landmarks, keep the
  box. Legacy code constructing `FaceDetection` without landmarks is
  unaffected.
* `tests/test_quality.py`: 56 deterministic tests with synthetic
  images (private-seeded RNG so expected metric values are
  order-independent) and real `cv2` metric computation; plus 4
  landmark regression tests in `tests/test_face_detection.py`.

### Verified

* Focused quality tests: 56 passed. Face-detection tests: 46 passed.
* `python -m pytest` → 271 passed (215 prior + 56 new) from the project
  root and an external working directory; stable across repeated runs.
* Fresh-process import hygiene: no camera, no model init, no state at
  import or assessor construction; no unrelated ML stack loaded.
* `git diff --check` → clean.

### Known limitations

* The orientation check is a geometric landmark proxy — **not** 3D
  pose estimation; cannot distinguish head rotation from facial-shape
  variation. Without landmarks (`pose_policy="optional"`) the check is
  skipped and the pose metrics stay `None`.
* Thresholds are initial, testable defaults; calibration on real
  enrollment footage is deferred to Phase 2 evaluation.

### Not included (per plan — later steps)

* Enrollment itself (Step 07), embeddings (Step 08), recognition,
  temporal verification, attendance logic, liveness, multi-frame
  quality decisioning.

## [0.2.0] — 2026-09-23 — Phase 1, Step 02: Configuration & constants

### Added

* `src/config.py`: centralized configuration and constants.
  * Frozen application constants: `APP_NAME`, `PROJECT_ROOT`
    (cwd-independent, derived from `__file__`), `CAMERA`,
    `FACE_PROCESSING`, `ENROLLMENT` (validated frozen dataclasses —
    the single place to tune thresholds in later steps).
  * Environment-specific `AppSettings` via `load_settings()`:
    `APP_ENV` (development/testing/staging/production), `DEBUG` (strict
    boolean), `DATA_DIR`, `LOG_DIR` (relative values anchored to the
    project root) plus derived paths (`employees_dir`,
    `face_samples_dir`, `embeddings_dir`, `models_dir`, `reports_dir`).
  * `ConfigError(ValueError)` fail-fast handling for invalid environment
    values; invalid constants raise `ValueError` at import.
  * `ensure_directories(settings)` for explicit directory creation —
    importing the module performs no side effects.
* `.env.example`: added `APP_ENV` and `DEBUG`; documented the
  environment-vs-constants split (camera/face/enrollment constants stay
  in `src/config.py`).
* `tests/test_config.py`: 18 tests mapped to the six Step 02
  requirements (import surface, path resolution incl. foreign-cwd
  subprocess, value availability and frozen-ness, env overrides,
  invalid-value handling, import side-effect freedom).

### Verified

* `python -m pytest` → 35 passed (17 Step 01 + 18 Step 02), from both
  the project root and an external cwd.
* Fresh-interpreter check: importing `src.config` imports no CV/ML stack
  and creates no directories.

### Not included (per plan — later steps)

* Camera service, detection, quality, enrollment logic, embeddings,
  recognition, temporal verification, attendance logic.

## [0.3.0] — 2026-10-09 — Phase 1, Step 03: Employee data model

### Added

* `src/employee.py`: the identity-layer `Employee` model.
  * `Employee` — frozen, slotted dataclass with `employee_id`,
    `full_name`, `is_active`, `created_at` and `to_dict()` explicit
    serialization (dictionary only — not persistence).
  * Validation: `EmployeeValidationError(ValueError)` with actionable,
    field-specific messages; no silent coercion of invalid input.
  * Normalization: `employee_id` trimmed only (entries like `"007"` and
    `"EMP-0042"` preserved exactly); `full_name` trimmed plus internal
    whitespace collapsed, case preserved; `is_active` must be a real
    `bool`; `created_at` defaults to UTC now and must be timezone-aware.
  * Identity boundaries honored: no face data, embeddings, camera
    frames, recognition confidence, or attendance state in the model;
    no CV/ML/database dependencies (ARCHITECTURE.md D6).
* `tests/test_employee.py`: 36 tests mapped to the nine Step 03
  requirements (creation, field validation, whitespace rejection,
  boolean strictness, normalization, ID preservation, timestamps,
  serialization, import hygiene).

### Verified

* `python -m pytest` → 71 passed (35 Step 02 + 36 Step 03) from the
  project root.
* External-cwd run → 71 passed.
* Fresh-interpreter check: importing `src.employee` imports no CV/ML
  stack and performs no side effects.

### Not included (per plan — later steps)

* Employee persistence/repository and `employee_id` uniqueness
  enforcement (storage steps + Phase 3).
* Camera service, detection, quality, enrollment logic, embeddings,
  recognition, temporal verification, attendance logic.

## [0.4.0] — 2026-10-09 — Phase 1, Step 04: Live camera service

### Added

* `src/camera.py`: `CameraService` — the frame source for downstream
  face detection.
  * `open()` raises `CameraOpenError` if the device cannot be opened or
    configured framing cannot be applied; `read_frame()` returns
    `(True, frame)` only for verified non-empty device frames and
    `(False, None)` on failure; `release()` is idempotent and never
    raises; `is_open` reflects actual device state.
  * Context-manager support: `__enter__` performs the open,
    `__exit__` always releases the capture — even if the `with` block
    raises — and never masks the in-flight exception.
  * `CameraStateError` for invalid lifecycle misuse (reading/opening
    in the wrong state); `CameraError` is the common base.
  * Configuration comes solely from `src.config.CAMERA` (index,
    frame dimensions); logging reuses the Step 01 `get_logger`
    contract.
* `tests/test_camera.py`: 30 hardware-independent tests with
  deterministic `cv2.VideoCapture` fakes covering: success/failure of
  open and read, capture release and idempotent release, context-manager
  cleanup on normal and exceptional exit, no handle leak on failed
  open, no fabricated/stale frames, configured index/framing honored,
  custom `CameraConfig` injection, lifecycle-state semantics, and
  import hygiene (no `VideoCapture` instantiation at import).

### Verified

* Focused camera tests: 30 passed.
* `python -m pytest` → 101 passed (71 prior + 30 new), from both the
  project root and an external working directory.
* Fresh-interpreter checks: importing `src.camera` instantiates no
  `cv2.VideoCapture` and imports no ML stack.

### Not included (per plan — later steps)

* Face detection, quality assessment, enrollment, embeddings,
  recognition, attendance logic; camera reconnection/background
  streaming/multi-camera management by design.

## [0.5.0] — 2026-10-09 — Phase 1, Step 05: Face detection

### Added

* `src/face_detection.py`: `FaceDetector` — locating faces in
  caller-supplied frames (no identification, no camera ownership).
  * Backend: `cv2.FaceDetectorYN` (YuNet ONNX) from the already-pinned
    opencv-python dependency — **no new dependency added**. Verified
    empirically that opencv-python 5.0.0.93 ships no cascade files and
    no YuNet model; the ONNX model is operator-supplied
    (`models/yunet.onnx`, never auto-downloaded, never auto-created).
  * `FaceDetector.load()` is explicit and idempotent;
    `FaceDetectionInputError` (a `ValueError`) covers invalid frames
    (None/empty/wrong-dim/channels/dtype/zero-size); frames are never
    mutated; `FaceDetectorLoadError` covers missing/corrupt models;
    `FaceDetectorError` covers backend failures — failures are never
    misreported as "no faces".
  * `FaceDetection` frozen result: `x, y, width, height` pixel box plus
    backend-supplied `confidence` (None when absent, never invented),
    clamped to frame bounds, fully-outside boxes dropped.
  * Detection threshold comes from `FACE_PROCESSING.detection_confidence`
    (Step 02); nothing hardcoded.
* `tests/test_face_detection.py`: 42 deterministic tests (typed stub
  backend for correctness paths, real-backend failure integration for
  missing/corrupt models, fresh-process import hygiene — no camera, no
  model load, no network, no filesystem writes at import).

### Verified

* Focused face-detection tests: 42 passed.
* `python -m pytest` → 143 passed (101 prior + 42 new), from both the
  project root and an external working directory.

### Not included (per plan — later steps)

* Face quality assessment, enrollment, embeddings, recognition,
  temporal verification, attendance logic. The YuNet model file itself
  must be supplied by the operator before real-world detection runs.

## [0.1.0] — 2026-09-23 — Phase 1, Step 01: Project structure & environment

### Added

* Git foundation: `.gitignore` covering virtual environments, `.env`
  secrets, biometric data (`data/employees/`, `data/face_samples/`,
  `data/embeddings/`), runtime logs, and model weights.
* Python 3.14 virtual environment; `requirements.txt` with pinned
  foundation packages: `numpy 2.5.3`, `opencv-python 5.0.0.93`,
  `python-dotenv 1.2.3`, `pytest 9.1.1`. All wheels install successfully
  on Python 3.14 (Windows x64).
* Project directory skeleton: `src/`, `notebooks/`, `data/{employees,
  face_samples,embeddings,logs}/`, `models/`, `tests/`, `reports/`,
  `docs/` (placeholders via `.gitkeep`).
* `src/logging_setup.py`: `get_logger(name)` providing an idempotent
  console + rotating-file logging setup (`data/logs/app.log`, 5 MB × 5),
  configured via `LOG_LEVEL` / `LOG_DIR`.
* `.env.example` documenting the environment keys actually consumed by
  the implemented code.
* `tests/test_environment.py`: 17 verification tests (venv detection,
  Python version, package imports, logging behavior, directory skeleton).
* `pytest.ini` with `pythonpath = .` for invocation-independent imports.
* Project documentation: `README.md`, `PROJECT_PLAN.md`,
  `ARCHITECTURE.md` (incl. decisions D1–D4), `DEVELOPMENT_STATUS.md`.

### Verified

* `pytest tests/ -v` → 17 passed (Python 3.14.7, venv interpreter).
* Bare `pytest` and external-cwd invocation → 17 passed.
* `git check-ignore` confirms generated/biometric paths are excluded.

### Not included (per plan — later steps)

* Camera capture, face detection, quality, enrollment, embeddings,
  recognition, temporal verification, attendance logic.
* Phases 2–7 (business rules, PostgreSQL/FastAPI, dashboard, RBAC,
  Docker, advanced liveness).
