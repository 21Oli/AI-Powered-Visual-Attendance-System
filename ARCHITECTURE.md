# ARCHITECTURE.md

Technical architecture and architectural decisions for the AI-Powered
Visual Attendance System. This file records decisions as they are made.

## 1. Core principle: Identity ≠ Recognition ≠ Attendance

The three concerns are deliberately separate components:

* **Identity** — who is this person? (enrollment records, face samples,
  embeddings per employee)
* **Recognition** — how confident are we that a detected face belongs to
  that person? (similarity scores, thresholds)
* **Attendance** — should this recognition create an attendance event?
  (temporal verification, business rules, duplicate protection)

Pipeline:

```text
Camera → Face Detection → Face Quality → Recognition → Confidence
       → Temporal Verification → Attendance Decision → Attendance Event
```

A recognized face does not automatically produce an attendance event.
Each stage is an independent module in `src/` and is individually testable.

## 2. Code organization

* `notebooks/` — experimentation and demonstration.
* `src/` — reusable production code. Stable logic graduates from
  notebooks into `src/`.
* `tests/` — pytest suite; every roadmap step ships a verification test.

## 3. Decisions log

### D1 — Python 3.14 with empirical verification (Step 01)

The development machine has Python 3.14.7 only. Wheel support for the
Phase 1 stack was verified on PyPI before choosing it:

* `onnxruntime` 1.30.0 — official `cp314` Windows x64 wheels.
* `mediapipe` 1.0.1 — `py3-none` Windows wheels (classifiers lag at 3.12;
  importability is verified empirically in Step 05).
* `opencv-python` 5.0.0.93 — installed and imported successfully on 3.14.

**Fallback policy:** if any future package fails to install or import on
3.14, recreate the venv on Python 3.12 (maximum CV-stack compatibility)
and pin the interpreter version in this document and the README.

**Package timing policy:** `requirements.txt` contains only the packages
the implemented steps actually use (currently numpy, opencv-python,
python-dotenv, pytest). `mediapipe` is added in Step 05 (face detection);
`insightface` + `onnxruntime` in Step 08 (embeddings). This keeps commits
honest and confines version risk to the step that introduces a package.

### D2 — Logging foundation is a separate, minimal module (Step 01)

`src/logging_setup.py` provides `get_logger(name)`: a console handler
(stderr) plus a rotating file handler under `data/logs/` (5 MB × 5
backups). Configuration is idempotent — repeated calls never duplicate
handlers. Level and directory come from `LOG_LEVEL` / `LOG_DIR` (see
`.env.example`). Step 02 (`src/config.py`) builds on this contract.

### D3 — Biometric data never enters Git

`data/employees/`, `data/face_samples/`, and `data/embeddings/` are
git-ignored. The repository contains only directory skeletons (`.gitkeep`).
Model weights under `models/` are likewise ignored (downloaded at runtime
by the libraries in later steps).

### D5 — Configuration: env-vs-constants split, fail-fast, no import side effects (Step 02)

`src/config.py` is the single source of truth for configuration:

* **Environment variables only for environment-specific values**:
  `APP_ENV`, `DEBUG`, `DATA_DIR`, `LOG_DIR`. Application constants
  (camera index/frame size, face-processing thresholds, enrollment
  sample counts) are frozen dataclass instances in `src/config.py` —
  they must not migrate into `.env`. `.env.example` documents this split.
* **Frozen everything**: all configuration objects are frozen
  dataclasses; there is no global mutable state. Future steps tune
  thresholds in this one module instead of scattering magic numbers.
* **Fail fast**: invalid constants raise `ValueError` at import; invalid
  environment values raise `ConfigError` (a `ValueError` subclass) from
  `load_settings()`.
* **Working-directory independence**: `PROJECT_ROOT` derives from
  `__file__`; relative env paths anchor to `PROJECT_ROOT`. Verified by a
  subprocess test importing the module from a foreign cwd.
* **No import side effects**: importing `src.config` creates no
  directories, opens no devices, and imports no CV/ML stack. Directory
  creation happens only via explicit `ensure_directories()` at startup.
* **Logging continuity**: Step 01's `src/logging_setup.py` contract is
  unchanged; `LOG_LEVEL`/`LOG_DIR` remain the interface between the two
  modules.

### D6 — Employee identity is a pure, frozen model (Step 03)

`src/employee.py` defines the identity layer's contract:

* **Pure standard library.** The model depends on nothing beyond the
  Python stdlib — no OpenCV, mediapipe, insightface, numpy, database
  drivers, or network services. Verified by a fresh-interpreter test.
* **Frozen dataclass.** Instances are immutable; the recognition and
  attendance layers later *reference* employees by `employee_id` but
  never mutate or extend this model with biometric fields (embeddings,
  face samples, confidence, attendance state).
* **Validation at construction**, never coercion: empty/whitespace
  IDs or names, non-boolean `is_active`, and naive `created_at`
  datetimes all raise `EmployeeValidationError` (a `ValueError`).
* **Minimal documented normalization**: `employee_id` is trimmed only
  (leading zeros, hyphens, underscores, and case are preserved —
  real business IDs must survive); `full_name` is trimmed with
  internal whitespace collapsed to single spaces, case preserved.
* `created_at` is timezone-aware only (defaults to `datetime.now(UTC)`).
* `to_dict()` is an explicit dict serialization for
  logging/tests/interchange — it is **not** persistence. Uniqueness of
  `employee_id` is a storage-layer concern enforced in later steps.

### D7 — Camera service: strict lifecycle, injected backend (Step 04)

`src/camera.py` defines the pipeline's frame source:

* **Single backend, injected for tests.** `cv2.VideoCapture` is the only
  camera backend (already a pinned dependency — no new frameworks);
  tests replace it deterministically at `src.camera.cv2.VideoCapture`
  so the suite runs without physical hardware.
* **Configuration flows one way**: all camera parameters come from
  `src.config.CAMERA` (Step 02); the module hardcodes nothing.
* **Fail fast, never fabricate**: a failed open raises
  `CameraOpenError` after releasing the half-initialized handle (no
  leak, clean retry); a failed read returns `(False, None)` — stale or
  synthetic frames are never returned to callers.
* **Lifecycle errors**: `CameraStateError` guards misuse (read before
  open, re-open while open); `release()` is idempotent, logs its own
  failures, and never masks an in-flight exception from a `with` block.
* **Import hygiene**: importing the module touches no device —
  acquisition happens only on explicit `open()` / `__enter__`.
* Deferred by design: reconnection, background streaming, multi-camera
  management, and liveness are later-phase concerns.

### D8 — Face detection: YuNet via FaceDetectorYN, operator-supplied model (Step 05)

`src/face_detection.py` adds the first recognition-pipeline component:

* **Backend**: `cv2.FaceDetectorYN` (YuNet) — verified empirically that
  opencv-python 5.0.0.93 ships no cascade XMLs and no bundled YuNet
  model, making legacy cascade usage impossible without a runtime
  download (not permitted). FaceDetectorYN is part of the existing
  pinned opencv wheel: **no new dependency**.
* **Model lifecycle**: the ONNX model file is operator-supplied at
  `models/yunet.onnx` and is never auto-downloaded or auto-created;
  `load()` is explicit, idempotent, and raises `FaceDetectorLoadError`
  with an actionable message when the file is missing or corrupt.
* **Contract**: frames are BGR uint8 `(H, W, 3)`; invalid inputs raise
  `FaceDetectionInputError` (a `ValueError` subclass); backend failures
  raise `FaceDetectorError` and are never misreported as "no faces";
  the caller's frame is never mutated.
* **Results**: `FaceDetection` dataclass — `(x, y, width, height)`
  pixel boxes, clamped to frame bounds; confidence is
  backend-supplied only (never invented), filtered when non-finite.
* **Configuration**: score threshold flows from
  `FACE_PROCESSING.detection_confidence` (Step 02); no constants are
  hardcoded in the detector module.

### D9 — Face quality: pure deterministic metrics, policy split (Step 06)

`src/quality.py` gates image quality between detection (D8) and
enrollment (Step 07):

* **Pure metric layer.** No camera (frames and `FaceDetection` results
  are caller-supplied), no model load, no filesystem, no network, no
  enrollment, no persistence. Importing or constructing the assessor
  performs no side effects.
* **Two explicitly separate concerns.** `assess_face()` is per-face
  *image* quality (size, Laplacian-variance sharpness, grayscale mean
  brightness, optional landmark orientation proxy);
  `assess_frame_faces()` is the *face-count policy* only
  (`no_faces` / `multiple_faces` / `None`). A policy-rejected
  multi-face frame never blocks per-face assessment — future phases
  can score each face independently.
* **Explicit decisions, stable reasons.** Results are frozen
  dataclasses with an `ACCEPT`/`REJECT` decision enum and a
  machine-readable `FaceQualityReason` naming the single first-failed
  check in a documented stable order. Measured metrics are always
  reported when computable — even on rejection; metrics that could
  not be computed stay `None` (never invented). Contract-violating
  inputs raise `FaceQualityInputError` (a `ValueError`), mirroring
  Step 05; valid-but-unusable inputs (out-of-frame box) are documented
  rejections, not exceptions.
* **Configuration centralization.** All thresholds live in the frozen
  `QualityConfig` (`src/config.py`, ARCHITECTURE.md D5): defaults are
  initial, testable calibration starting points — never claims of
  production biometric accuracy — and are tuned in Phase 2 in exactly
  one place.
* **Pose honesty.** The orientation check is a geometric landmark
  proxy (nose-tip offset from the eye midpoint, normalized by box
  width/height) — **not** 3D pose estimation. Without landmarks the
  check is skipped (`pose_policy="optional"`, metrics `None`) or
  rejects (`pose_policy="required"`, `no_landmarks`); values are
  never synthesized.
* **Backward-compatible detector extension.** `FaceDetection` gained
  an optional `landmarks` field populated only from real backend rows
  (15-column YuNet format); legacy 5-column behavior and construction
  sites are unchanged, guarded by regression tests.

### D10 — Enrollment: bounded session orchestration, staged persistence (Step 07)

`src/enrollment.py` consumes Steps 03–06 and hands accepted face
samples to Step 08. It owns no new algorithms — only orchestration,
limits, and safe persistence:

* **Injectable pipeline.** Camera factory, detector, assessor,
  enrollment config, and `AppSettings` are constructor parameters;
  tests run without a webcam, the YuNet model, or any real data
  directory. Importing or constructing the service performs no
  camera/model/filesystem side effects; directory creation happens
  only inside a real persistence run.
* **Explicit statuses, non-overloaded errors.** COMPLETED /
  CANCELLED / FAILED never collapse into a boolean. Bad identity input
  or traversal-shaped IDs raise `EnrollmentInputError` before capture;
  existing enrollment data raises `DuplicateEnrollmentError` before
  the camera opens; staging/publish failures raise
  `EnrollmentPersistenceError` after best-effort cleanup. Camera
  read failures and quality/face-count rejections are *attempts*
  (bounded by `EnrollmentConfig.max_capture_attempts`), while camera
  open errors and detector/assessor exceptions are fatal session
  errors — a hardware failure can never be misread as "keep trying".
* **Bounded sessions.** `min_samples` → COMPLETED; caller
  `should_cancel` (polled before every attempt) → CANCELLED with
  nothing persisted, even mid-session after the minimum was reached;
  attempt budget exhausted → FAILED. Release happens on every exit
  path.
* **Traversal-proof, deterministic storage.** Employee IDs are
  percent-encoded into directory names (dots encoded → no `..`) while
  the manifest carries the exact ID (`format_version: 1`). Existing
  enrollments are never overwritten; enrollment existence is defined
  by the published manifest, published samples-first via rename and
  manifest-last via `os.replace`.
* **Biometric hygiene.** Face crops stay in the already-ignored
  `data/face_samples` tree; no image data is logged, echoed, or
  uploaded; on failure only current-attempt artifacts (tracked paths)
  are removed; pre-existing and unrelated employees are untouched.
  Duplicate handling is byte-level only — biometric similarity is
  explicitly a Step 08+ concern.

### D11 — Face embeddings: SFace crop-only generation, validated outputs (Step 08)

`src/face_embedding.py` turns accepted face crops into validated
float32 vectors — generation only (matching is Step 10, persistence
is Step 09):

* **Backend choice: SFace (`cv2.FaceRecognizerSF`), with evidence.**
  The documented insightface ArcFace path (`ArcFaceONNX.get` →
  `face_align.norm_crop`) *requires five landmarks*; Step 07
  deliberately persists plain crops without landmarks or source
  frames, so that path is not usable on the existing data as-is.
  SFace's documented preprocessing (crop → 112×112 blob, scale 1,
  mean 0, BGR→RGB) works directly on a plain crop, is Apache-2.0
  licensed (D4), and uses the DNN loader bundled in the pinned
  opencv-python wheel — **no new dependency, no `onnxruntime`**.
* **Crop-only honesty.** The alignCrop/landmark workflow remains the
  reference accuracy path; without alignment, SFace embedding
  accuracy is unknown until measured on real footage (Phase 2).
  Documented as a limitation rather than hidden.
* **Explicit lifecycle, injectable inference.**
  `FaceEmbedder(model_path, expected_dimension, normalize_output,
  backend_factory)`; `load()` is explicit and idempotent;
  construction/import perform no camera/model/filesystem/network
  side effects. The ONNX artifact is operator-supplied at
  `models/sface.onnx` (`DEFAULT_MODEL_PATH`), never downloaded.
* **Typed errors, validated outputs.** `FaceEmbeddingInputError`
  (ValueError) for bad crops (None/empty/wrong shape/dtype);
  `FaceEmbeddingModelError` for missing/invalid artifacts and init
  failures; `FaceEmbeddingOutputError` for inference/output failures
  (non-finite, wrong shape, contract dimension mismatch, zero norm —
  rejected, never divided by zero). L2 normalization happens only
  when `normalize_output=True` (contract-driven). Results are frozen
  with write-protected float32 vectors so callers can never mutate
  backend state. Error messages carry no image or vector content.
* **Biometric hygiene.** No persistence in this step; nothing is
  logged, uploaded, or written; Step 07's `.gitignore` protections
  are unchanged; tests use only synthetic crops and injected fakes.

### D12 — Embedding storage: validated per-employee record files, no silent overwrites (Step 09)

`src/embedding_storage.py` persists Step 08's validated embeddings
under the existing `AppSettings.embeddings_dir` (`data/embeddings/`,
D3) without competing directories or identity sources:

* **Format: `.npz` + JSON sidecar.** One array file per employee
  (`np.savez` write, `np.load(allow_pickle=False)` read — pickle and
  object arrays are structurally excluded) plus a small validated
  JSON sidecar (schema `format_version: 1`: exact employee ID,
  declared dimension, dtype marker, UTC `stored_at`). Chosen over a
  text codec for value-exactness and over SQLite/any DB for zero new
  dependencies. Face images are never part of embedding storage.
* **Single identity source-of-truth.** IDs are validated through the
  existing `Employee` model rules and Step 07's traversal-proof
  percent-encoding (dots encoded); enrollment manifests are never
  read or modified, and no employee is created by a storage request.
  Verifying that a recognized match is an enrolled employee is left
  to Step 10's policy.
* **Validation before write, re-validation on read.** Save rejects
  non-1-D, empty, non-finite (NaN/±inf), non-zero-norm violations and
  enforces the constructor's `expected_dimension` contract on save
  and load (cross-model dimension mismatches are typed format
  errors, never silent feature-space mixing). Load treats files as
  untrusted input and re-checks schema, ID association, readability,
  dtype, shape, and metadata-vs-array agreement on every call;
  corruption raises typed errors instead of degrading into an empty
  vector or an inclusive not-found answer.
* **No silent overwrite.** A record exists iff both files exist;
  `save()` refuses by default and `replace=True` is the explicit,
  documented update path. Non-replacing publication hard-links staged
  files into place so a concurrent winner surfaces as a duplicate
  error rather than being clobbered; replacing publication is one
  `os.replace` per file, array first and sidecar last, so a crash can
  leave an unpublished array but never a declared record without
  validated metadata. Failed attempts clean only their own artifacts.
* **Biometric hygiene.** Embeddings live only in the already-ignored
  tree; nothing is logged (counts and IDs only), uploaded, or echoed;
  tests use synthetic vectors in temporary directories. Returned
  records are frozen with write-protected vectors.

### D13 — Recognition: cosine decision over Steps 08/09, provisional thresholds, fail-closed eligibility (Step 10)

`src/face_recognition.py` implements the recognition stage between
storage (D12) and temporal verification (later phases):

* **Pure cosine decision, honest scale.** score = `a·b / (‖a‖‖b‖)`
  over validated float64 dot products, in [-1, 1]; inputs validated
  (1-D, non-empty, finite, equal dimensions, non-zero norms) on both
  query and candidates — zero-norm/NaN/inf vectors raise typed errors
  instead of producing scores, and no Euclidean distance or borrowed
  threshold is used. Because Step 08's crop-only mode stores **raw**
  embeddings, the literature's 0.6 *normalized*-feature threshold is
  deliberately NOT reused; `RecognitionConfig.min_similarity=0.36`
  and `min_margin=None` are documented **provisional** starting
  points requiring Phase 2 empirical calibration (D5 conventions).
* **Deterministic decision procedure.** Eligible candidates ranked by
  `(-score, employee_id)`; `NO_CANDIDATES` when nothing is eligible;
  `UNKNOWN` when best < threshold; `AMBIGUOUS` when a configured
  margin separates top two too narrowly; `MATCHED` otherwise.
  Boundary semantics are explicit (best == threshold ⇒ MATCHED;
  margin == boundary ⇒ MATCHED; equal top scores ⇒ AMBIGUOUS — ties
  never resolve into identity). Unknown/ambiguous results never
  carry an employee ID; results carry scores, status and a short
  reason — never crops or vectors.
* **Eligibility from existing authorities (no new identity store).**
  Candidates must have a published Step 07 enrollment manifest
  (the "enrollment exists" marker) and pass the active gate — trusted
  from the manifest's recorded `is_active` (Step 07 semantics) or
  overridden by an injectable `active_lookup`. Manifests are untrusted
  input: unreadable/malformed/schema-mismatched/identity-contradicting
  manifests raise `ManifestIntegrityError` (fail-closed default;
  documented `"skip"` policy is the operator escape hatch). Inactive
  employees are excluded from decisions — never deleted or modified.
* **Failure honesty.** Corrupt stored records (explicit candidate
  requests) surface as their typed Step 09 errors; a record vanishing
  between listing and load raises `RecognitionStorageError`; embedder
  failures raise `RecognitionModelError` — none of these ever
  degrade into an ordinary unknown-person result.
* **Model compatibility honesty (D12 unchanged).** Step 09 sidecars
  record no model identifier and none was invented; dimension
  equality is not claimed to prove provenance. The documented remedy
  is operational: one known, consistent embedding-model generation
  per deployment (re-embed after model changes). No metadata format
  change was made — existing records stay byte-compatible.
* **Composition not bypass.** The recognizer uses only the public
  Step 09 storage API (`list_employee_ids`/`load`) plus a public
  `safe_dir_name` wrapper over Step 07's encoder so eligibility reads
  and writes share one path scheme; private storage files are never
  read directly. Auto-listed corrupt sidecars follow Step 09's
  documented skip-policy (tested composition: NO_CANDIDATES, never
  an identity decision).
* **Privacy & scope.** All computation is local; logs/results carry
  IDs, statuses, reasons and scores only. No temporal verification,
  liveness, unknown-face capture, attendance events, camera loop, or
  persistence of recognition results; tests are fake-backed (no real
  SFace artifact) and no recognition-accuracy claim is made.

### D14 — Temporal verification: conservative streak policy over Step 10 results, bounded state, one sequence per instance (Step 11)

`src/temporal_verification.py` implements the temporal stage between
single-frame recognition (D13) and attendance decisions (Step 13):

* **Composition not duplication.** The verifier consumes existing
  `RecognitionResult` values one at a time and invents no matching,
  thresholds, storage, or manifest logic; a `MATCHED` result without
  a valid employee ID is refused with a typed error (contract
  violation — never de-escalated into non-evidence).
* **Conservative evidence policy.** Only consecutive `MATCHED`
  frames for the **same** employee ID build a streak;
  `UNKNOWN`/`AMBIGUOUS`/`NO_CANDIDATES` are never positive evidence
  and **interrupt** it (uncertain instants cannot contribute to or
  preserve a verification). A matched frame for a different ID
  discards the old streak (cross-ID evidence never combines).
  `VERIFIED(employee_id)` requires `min_consistent_frames`
  consecutive consistent frames; interrupted results carry
  `employee_id=None` so prior evidence can never be re-reported as a
  newly verified identity.
* **Bounded, minimal state.** Retained evidence is one candidate ID,
  one counter, and a bounded deque of at most `window_frames` recent
  statuses (deterministic oldest-first advance). No frames, crops,
  embeddings, scores, or names; logs carry counters/statuses/reasons
  only — never IDs or biometric data. IDs are preserved verbatim
  (`"007"`, `"EMP-0042"`). No persistence, camera, model, or network
  at import or construction time (subprocess-verified).
* **Session semantics.** One verifier manages one tracking sequence;
  the caller owns one instance per tracked face / camera session
  (instances share no state; `reset()` is instance-scoped and clears
  streak + window). Frame order is the caller's feeding order — no
  sequence numbers are invented. Post-verification frames are
  idempotent state reporting (no recurring verification events);
  verification ends the moment a non-MATCHED frame arrives, so an
  identity cannot remain verified without new evidence.
* **Provisional defaults, honest scope.**
  `TemporalVerificationConfig(min_consistent_frames=3,
  window_frames=10)` are initial engineering defaults requiring
  Phase 2 real-camera calibration; strict validation rejects
  bool/float and impossible (`window < min_consistent`) settings.
  Temporal verification is **not** an attendance event and no
  real-camera accuracy is claimed.

### D15 — Unknown-face handling: conservative outcomes over Steps 10/11, bounded unresolved-episode policy, no identity leakage (Step 12)

`src/unknown_face_handler.py` sits between temporal verification
(D14) and future attendance decisions (Step 13), doing nothing but
handling unresolved faces conservatively:

* **Trust boundary.** `observe_temporal()` is authoritative: only a
  `VERIFIED` temporal result is treated as a temporally verified
  presence (`NO_ACTION`, or `EPISODE_ENDED` when it resolves an
  active episode) and the outcome carries no identity;
  `COLLECTING`/`INTERRUPTED` stay unresolved observations.
  `observe_recognition()` is observability-only: raw `MATCHED` is
  classified `MATCH_UNVERIFIED` — a single-frame match is never an
  identity here. No entry point can assign an identity to an unknown,
  ambiguous, or unverified face, and attendance events are not
  generated.
* **Deterministic episode/cooldown policy.** One `OBSERVED` alert
  opens an episode per caller-managed session key; unresolved frames
  within `episode_cooldown_seconds` of the alert are `SUPPRESSED`
  (countered); the cooldown does not refresh and the episode expires
  exactly at alert + cooldown (lazy, injectable monotonic clock), so
  stale state never suppresses observations indefinitely, and a
  `VERIFIED` result or `reset()` ends an episode explicitly.
* **Bounded minimal state, no person claims.** At most `max_sessions`
  tiny episode records (LRU eviction); no frames, crops, embeddings,
  names, IDs, scores, or audit trail are retained or persisted.
  Without a stable face-track identifier, suppression is a
  caller-managed session policy — repeated suppressed frames are not
  proof of one person, and unknown people are never merged or
  distinguished by appearance.
* **Fail-safe inputs, limits documented.** Typed errors for wrong
  result types, synthetic bogus statuses, contract violations
  (`MATCHED`/`VERIFIED` without identity), oversized/whitespace
  session keys (never truncated), and non-callable clocks; defaults
  (`UnknownFaceConfig`) are engineering starting points pending
  Phase 2 calibration; real-camera integration remains future work.

### D4 — Dependency licensing note

`insightface`'s library code is MIT, but its pretrained model packages
(buffalo/raccoon families) are licensed for **non-commercial research
only**. This is acceptable for a portfolio/educational project; a
production deployment would require commercially licensed models or
self-trained embeddings. Recorded here so the constraint is visible
before Step 08.
