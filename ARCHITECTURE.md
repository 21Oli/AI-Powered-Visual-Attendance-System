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

### D16 — Attendance-event engine: temporal-verification trust boundary, bounded non-refreshing check-in cooldown, clock separation, no durable storage (Step 13)

`src/attendance.py` converts temporally verified identities into
structured check-in decisions and preserves the identity /
recognition / attendance separation:

* **Trust boundary (recognition never becomes attendance).** Only a
  structurally valid `VERIFIED` `TemporalVerificationResult`
  authorizes a decision; `COLLECTING`/`INTERRUPTED` yield
  `NOT_VERIFIED`; raw `RecognitionResult` values are rejected input,
  so a single-frame `MATCHED` can never create an event. Malformed
  results, invalid clock outputs, and contract violations fail
  safely with typed errors — never a fabricated event; inputs are
  never mutated and IDs stay verbatim (`"007"`, `"EMP-0042"`). An
  optional injectable `active_lookup` (the Step 10 hook pattern, no
  new manifest/storage reads) gates inactive employees
  (`INACTIVE_EMPLOYEE`); nothing is invented when the hook is absent.
* **Event & decision schema.** Frozen `AttendanceEvent(event_id,
  employee_id, event_type=CHECK_IN, occurred_at)` with the timestamp
  normalized to UTC (naive datetimes rejected) and a documented,
  validated `uuid4().hex` ID default (injectable factory); frozen
  `AttendanceDecision` exposes the event only on `EVENT_CREATED` —
  suppressed or unverified decisions never fabricate one. No crops,
  frames, embeddings, similarity diagnostics, or unknown-person data
  are carried or logged (statuses/counts only).
* **Duplicate suppression: in-memory, bounded, non-refreshing.** One
  `CHECK_IN` per employee per `check_in_cooldown_seconds=300.0`; the
  anchor is the created event's monotonic instant; boundary exact
  (elapsed `< cooldown` suppressed, `== cooldown` accepted); repeated
  frames never refresh the anchor, so the interval expires
  deterministically; employees tracked independently; anchors LRU-
  bounded at `max_tracked_employees=64` (worst case after eviction:
  one extra event); `reset()` clears one or all anchors. **Honest
  scope:** protection holds only for the engine-instance lifetime —
  restarts lose it; this is explicitly NOT durable storage,
  database-level, or cross-process exactly-once delivery, and no
  audit trail is written.
* **Explicit clock separation.** `clock_wall` (timezone-aware, naive
  and non-datetime outputs rejected) produces event timestamps;
  `monotonic_clock` (finite outputs enforced) governs suppression —
  backward wall-clock movement can neither bypass duplicate
  protection nor expire anchors. Defaults (`AttendanceConfig`) are
  initial engineering values, not calibrated workplace policies;
  real attendance business rules (check-out, shifts, lateness,
  daily dedup) and durable storage belong to Phase 2 / later
  approved steps. Big picture: the future orchestration layer feeds
  Step 11 verified results into the engine and consumes the frozen
  decision; the engine is not wired into the camera loop.

### D17 — Step 14 evaluation methodology: software regression vs empirical CV status, privacy-safe notebook, evidence labeling (Step 14)

`notebooks/09_phase1_evaluation.ipynb` +
`reports/phase1_evaluation.md` record Phase 1 evaluation evidence
under one rule: **every reported number is labeled** MEASURED /
TEST COVERAGE / NOT RUN / BLOCKED / NOT MEASURED, with its exact
reason when evidence is unavailable.

* **Two-track status (never conflated).** Software regression is
  deterministic (654 tests, no camera/model/network) and is the
  bankable signal; empirical computer-vision evaluation (detection
  precision/recall on annotated ground truth, quality under real
  lighting/pose, FAR/FRR recognition accuracy, spoof resistance) is
  **BLOCKED** on artifacts (sface.onnx) and consented labeled data —
  a missing-artifact environment record, not a code failure, and
  never a reason to weaken tests or fabricate metrics.
* **Notebook safety contract.** No camera opened at run time, nothing
  enrolled, production `data/` never read or written (all
  embedding experiments run in private temp dirs), no biometric
  prints, and no downloads; the only real-model cell (local
  `yunet.onnx` on a synthetic frame) is explicitly opt-in, and real
  content measurements are deferred until consented datasets exist.
* **Metrics honesty.** The IoU matching criterion for future
  detection evaluation is pre-defined in the notebook; thresholds
  are exercised with synthetic rows only and are labeled provisional
  (D5); no FAR/FRR or accuracy is ever inferred from synthetic unit
  tests; duplicate-suppression correctness is in-software and
  explicitly not a durable/cross-process exactly-once claim (D16).

### D19 — Durable attendance composition: engine and store stay authoritative, four explicit outcome states, no automatic retry (Phase 2, P2-2)

`src/attendance_persistence.py` (`DurableAttendanceService`) composes
the Step 13 engine and the P2-1 store without changing either. The
single new decision is what to do when a created event fails to
persist — answered explicitly, never by presenting an uncertain write
as a durable check-in.

* **Composition, not duplication.** The temporal result is passed to
  the engine unmodified and the decision is consumed as produced; the
  event object, if any, is appended exactly once via the store's
  public `append`. No decision logic, cooldown, threshold, or schema
  logic is re-implemented here. Collaborators are validated
  structurally (callable `update`/`append` — the engine's injectable
  pattern), so doubles can replace them in tests; construction does
  no filesystem work.
* **Four explicit outcome states** on the frozen
  `DurableAttendanceOutcome`: `NO_EVENT` (engine decided without an
  event — nothing appended, decision carried verbatim), `PERSISTED`
  (append returned; fsync confirmed), `PERSISTENCE_REJECTED`
  (append raised `AttendanceStoreInputError` — a **confirmed**
  failure: the store's input rejections occur before any write, so
  the journal is provably unchanged and the record is definitively
  absent), and `PERSISTENCE_UNCONFIRMED` (any other store error — an
  **uncertain** outcome: bytes may or may not have reached the
  journal). Both failure states carry the original exception object
  unmodified alongside the exact event and a short reason.
* **No automatic retry (deliberate).** After an uncertain append the
  journal may already hold the record (appends cannot be retracted),
  so a silent re-append could duplicate it; reconciliation by
  `event_id` belongs to the caller/operator layer. Engine input
  errors propagate unchanged (no append); only the store's documented
  error family maps to the unconfirmed state — other store exceptions
  propagate unlabeled as programming errors.
* **Scope honesty.** Live mode, smoke mode, and the CLIs are not
  wired to this service in P2-2 (no caller yet); cooldown anchors
  remain engine-instance-lifetime in P2-2 (restart recovery arrived
  in P2-3, D20); check-out/daily-dedup business rules remain future
  Phase 2 steps.

### D4 — Dependency licensing note

`insightface`'s library code is MIT, but its pretrained model packages
(buffalo/raccoon families) are licensed for **non-commercial research
only**. This is acceptable for a portfolio/educational project; a
production deployment would require commercially licensed models or
self-trained embeddings. Recorded here so the constraint is visible
before Step 08.

### D18 — Durable attendance event store: append-only JSON Lines journal, enforced five-key envelope, salvage-or-truncate torn-tail recovery (Phase 2, P2-1)

`src/attendance_store.py` (`AttendanceEventStore`) closes the Step 13
"no durable storage" gap at the storage layer only — it consumes
already-validated `AttendanceEvent` values and performs no face
processing, no engine logic, no camera work, and (in this step) no
live-mode wiring.

* **Format and privacy envelope (enforced, decorative-free).** One
  UTF-8 JSON object per line at `<DATA_DIR>/attendance/events.jsonl`
  with exactly `schema_version: 1`, `event_id`, `employee_id`,
  `event_type`, `occurred_at` (ISO-8601, UTC). The five-key set is
  enforced symmetrically: appends serialize nothing else, and loads
  reject extra keys outright — biometric, diagnostic, or merely future
  fields are typed corruption, never silently reinterpreted. No
  crops/images, embeddings, similarity scores, or unknown-person data
  are written or read. IDs stay verbatim (`"007"` stays `"007"`).
* **Append policy — safe append, not rewrite-per-event.** Normal
  appends use `open("a")` (O_APPEND) + `flush()` + `os.fsync()`, one
  record at a time; the whole-list rewrite cost grows with journal
  size and was explicitly not chosen. A record is "persisted" only
  after fsync returns; every failure raises
  `AttendanceStorePersistenceError` and the caller must treat the
  event as **unconfirmed**. Tested honesty: an fsync failure cannot
  retract already-buffered data, so the record may still be present —
  the store reports the uncertainty instead of pretending.
* **Torn-tail recovery (documented, never silent mid-file
  discards).** After an interrupted append, loading excludes an
  incomplete trailing fragment and *reports* it
  (`incomplete_trailing_record=True`); corruption of any other kind —
  malformed JSON, unknown schema version, invalid field, empty line —
  raises with the exact journal line number. Appending over a torn
  tail repairs it first: a trailing fragment that is a fully valid
  record is salvaged by appending the missing newline (pure append,
  record preserved); otherwise the complete-line prefix is
  republished via staged temp file → fsync → atomic `os.replace`
  (the Step 09 pattern) — the store's only rewrite path. A failed
  repair raises and leaves the journal byte-unchanged (tested), so
  mid-file corruption can never arise from the repair itself.
* **Validation policy.** Loading re-derives each record through the
  frozen `AttendanceEvent` constructor after schema checks; naive
  timestamps, unknown event types, wrong types, empty IDs, missing or
  duplicate keys are typed errors carrying line numbers. A missing
  journal is the normalized empty history; empty-but-present content
  is corruption, not emptiness. Unauthenticated, untrusted stored
  lines are treated with the same rigor as Step 09's sidecars.
* **Explicit non-goals (honest scope).** No cross-process file
  locking, no `event_id` uniqueness scan (retry/once policy belongs
  to the composition layer), fsync covers the file — not the
  directory entry — and there is no query/audit API yet (a later P2
  tool reads the journal read-only). Store touches no thresholds, no
  engine behavior, and no live-mode wiring in P2-1.

### D20 — Restart recovery of cooldown anchors: read-only replay, wall-to-wall ages, explicit clock-skew policy (Phase 2, P2-3)

`src/attendance_recovery.py` (`recover_cooldown_anchors`) replays
persisted attendance events into a fresh engine's cooldown state so
duplicate suppression survives a restart. Replay is read-only and the
engine stays the single cooldown authority; the journal schema, the
P2-1 store, and the P2-2 outcome contract are unchanged.

* **Additive engine API only.**
  `AttendanceEngine.restore_suppression_anchor(employee_id,
  *, elapsed_seconds)` places one anchor at
  `monotonic_now - elapsed_seconds` in the *current run's* monotonic
  domain and returns `False` when the age already meets or exceeds
  the configured cooldown (expired — equivalent to no anchor, nothing
  stored). Validation, bounded-state (LRU eviction beyond
  `max_tracked_employees`), and the cooldown constant are exactly the
  engine's own; a wall timestamp is never stored as or compared as a
  monotonic value.
* **Replay semantics (deterministic).** The journal is loaded in full
  through the store's `load_events()` **before** any engine mutation,
  so a failed load can never leave a partially recovered engine;
  `CorruptAttendanceJournalError` /
  `UnsupportedJournalSchemaVersionError` propagate unchanged (a failed
  load is never treated as an empty journal). Only `CHECK_IN` records
  count (explicit allowlist — future event types never silently
  affect check-in suppression); per employee the latest record by UTC
  `occurred_at` wins, ties resolved by journal position (later wins) —
  deterministic under out-of-order timestamps and under the store's
  documented duplicate-`event_id` retry limitation (duplicates carry
  identical timestamps and collapse to one anchor). Restores apply in
  first-seen journal order. A torn trailing fragment is excluded by
  the store's documented load semantics and never restores an anchor;
  the `incomplete_trailing_record` flag is passed through on the
  frozen `CooldownRecoveryReport` (restored IDs, expired and clamped
  counts, records scanned). No event is created and nothing is
  appended; the journal bytes are never modified.
* **Clock-skew policy (explicit, conservative).** Ages are measured
  wall-to-wall (`wall_now - occurred_at`, both UTC-aware; `wall_now`
  is supplied by the caller — recovery reads no hidden clock) and
  translated once into the monotonic domain. Wall clock moved
  **backward** across a restart (event dated after `wall_now`): the
  negative age is clamped to zero — the employee is suppressed for
  the full cooldown from the recovery instant, erring on the
  no-duplicate side (worst case one *delayed* legitimate check-in,
  never a duplicate) and preserving the engine's anchor≤now
  invariant. Wall clock moved **forward**: the age inflates and an
  expired cooldown is simply not restored — worst case one additional
  check-in, exactly the engine's documented bounded-state worst case;
  no invented compensation is applied. Honest limit: cross-run
  cooldown precision is bounded by wall-clock reliability between
  runs; within a run, suppression remains monotonic-exact.
* **Scope honesty.** Recovery is not wired into live mode (no
  caller); it neither retries writes nor appends anything, and its
  log lines carry counts only — never employee IDs, never biometric
  content.

### D21 — Read-only attendance report CLI: deterministic operator inspection, three-exit contract, UTC half-open filters (Phase 2, P2-4)

`tools/attendance_report.py` is an on-demand, **strictly read-only**
inspection utility over the P2-1 journal. It reuses
`AttendanceEventStore.load_events()` and the event model unchanged,
adds zero decision logic, and never appends, truncates, repairs,
rewrites, or locks the journal (byte-identity asserted in every
journal-touching test). It is **not wired into the live attendance
workflow** (the live demo remains unconnected to persistence).

* **Deterministic presentation.** Listing is chronological by default
  with `(occurred_at, original_index)` as the stable sort key — equal
  timestamps resolve by journal position, the same documented
  tie-break the recovery selection uses. `--journal-order` is the
  explicit append-order alternative. Duplicate `event_id` records
  print as-is (the store's documented retry limitation is reported,
  not silently hidden by de-duplication).
* **UTC half-open date filters.** `--since D` is inclusive at D's
  00:00:00Z; `--until D` covers D's whole day, ending exclusive at
  the next midnight (so fractional seconds such as 23:59:59.999999Z
  are covered without `<=` microsecond edge arithmetic); `--day D` is
  `[midnight, next midnight)`, exclusive with the pair. The cached
  bound covers all events through the day, removing `-1 microsecond`
  fragility. Site-local timezone display/filtering is intentionally
  unsupported in this first version (the journal stores UTC only) —
  a documented limitation, not an oversight. Employee filtering is
  verbatim (IDs never normalized, `"007"` stays `"007"`), repeatable,
  exact-match only.
* **Three-value exit contract (argparse's default exit overridden;
  only `error()` is subclassed so `--help` keeps the ordinary
  `SystemExit(0)` path).** `0` success — including an empty journal
  (a normal status, never an error) and an incomplete trailing
  record (the loader's excluded-fragment decision is *surfaced* as a
  stderr warning while valid rows print; the CLI repairs nothing).
  `2` journal read failure — corrupt mid-file lines, unsupported
  schema versions, unreadable data: the store's typed error is
  reported on stderr and NO event rows print (a failed load is never
  presented as an empty report — mirroring the P2-3 recovery
  principle). `3` invalid arguments or dates, all validated before
  any journal access (`YYYY-MM-DD` shape is enforced strictly so only
  one canonical spelling parses; `--day` cannot combine with
  `--since`/`--until`; an `--until` before `--since` is rejected)
  — the journal is never opened on that path.
* **Import/execution hygiene (enforced, tested).** The tool module
  imports no `src` modules at module level: the P2-1 store import is
  deferred into `main()` because the store's module graph
  transitively includes recognition-layer types (via the attendance
  engine's temporal-verification import). Executing a report imports
  the store — **necessary to reuse its loader unchanged — but never
  constructs a camera, detector, or recognizer model**, proven by a
  test that turns `cv2.VideoCapture` / `cv2.FaceDetectorYN` /
  `cv2.FaceRecognizerSF` / `onnxruntime.InferenceSession` into hard
  failures for the duration of a full report run, plus a subprocess
  assertion that plain import places no `cv2`/biometric module in
  `sys.modules` and creates no data-directory tree. Importing has no
  filesystem side effects; stdlib-only (no new dependencies; no
  `src/`, `requirements.txt`, or `pytest.ini` changes).
* **Summary and privacy.** `--summary` reports per-employee × per-day
  check-in counts in day order, then verbatim employee ID. Output
  carries exactly the four schema fields (UTC timestamp, event type,
  employee ID verbatim, event ID verbatim) — no scores, names,
  embeddings, or biometric content exist in the journal to print.
* **Scope honesty.** Not wired into the live attendance demo (which
  remains unconnected to persistence); no check-out/shift/daily-dedup
  business rules (a separate milestone, sequence unchanged); no
  stateful rendering, export formats, or cross-run history beyond the
  journal itself.

### D22 — Opt-in durable composition in the integration demo: one default-off flag, recovery before any event, fail-closed, unchanged default behavior (Phase 2, P2-5)

Step 25 wires the existing Phase 2 components (P2-1 store, P2-2
`DurableAttendanceService`, P2-3 recovery, P2-4 report CLI stays an
on-demand tool) into the integration demo's two execution paths behind
**one explicit `--durable` flag shared by `--smoke` and `--live`** —
default **off**. This is wiring only: no decision logic, threshold,
schema, store contract, outcome state, or unknown-face behavior
changed, and nothing in Phase 2 gained a new caller in the default
paths.

* **Default-off byte discipline (probe-tested).** Without the flag no
  store is ever constructed, `recover_cooldown_anchors` is never
  called, no journal directory is created, and smoke/CLI output is
  exactly the pre-P2-5 output. The flag is not mode-specific and its
  parsing never touches the camera or biometric models; live mode's
  other options are provably unaffected by its presence.
* **Recovery before any event (P2-3 ordering, enforced by
  construction).** Durable runs replay persisted cooldown anchors
  **before** the first attendance-processing call — before any frame
  is processed in smoke mode and before the detector loads or the
  camera opens in live mode. A journal-load or recovery error
  (`CorruptAttendanceJournalError`, unsupported schema versions, any
  store failure) therefore fails closed: smoke exits `1` with the
  typed error surfaced cleanly, live raises before anything is
  acquired. Durable mode never silently degrades to non-durable
  processing, and uncertain appends (`PERSISTENCE_UNCONFIRMED`) are
  reported and never automatically retried — the P2-2 four-state
  contract is consumed verbatim, never redefined. Recovery/report
  summary lines carry counts and statuses only — never employee IDs,
  never biometric content.
* **Deterministic where testable, honest where not.** `run_smoke` and
  `run_live` accept an injectable timezone-aware `wall_clock` (default
  `datetime.now(UTC)`) shared by the engine's event timestamps and the
  recovery age computation; the smoke journal is scoped to the run's
  temporary settings (removed with it), the live journal is the
  configured `data/attendance/events.jsonl`. Smoke-mode durable
  behavior is fully covered hardware-free (exactly one persisted
  event, positional-and-timestamp agreement, coupon boundary 299 s
  vs. exact 300 s, one-attempt persistence failures, restart
  simulation); live durable behavior is verified only through injected
  camera/detector doubles (recovery-before-hardware ordering, clean
  run writes nothing) and requires a real owner-supervised
  `--live --durable` session before any real-camera durability claim.
* **Privacy unchanged.** No unknown-face biometrics are stored (the
  unknown-face path is untouched); no frames, crops, embeddings, or
  similarity scores are logged; journal records keep the exact
  five-key envelope; live durable console lines add persistence
  statuses for already-verified frames only. Empirical CV accuracy
  evaluation remains BLOCKED pending consented labeled data; the
  check-out/shift/daily-dedup business-rules milestone remains
  explicitly future work.

### D23 — Check-out events, three independently bounded engine maps, journal-backed session recovery, one-snapshot startup (Phase 2, P2-6)

P2-6 adds `EventType.CHECK_OUT` (serialized value `check_out`) and
symmetric-but-independent state without changing the event envelope,
the journal schema version (still 1), or the decision authority.

* **Independent bounded state (S2).** The engine keeps three separate
  maps — check-in cooldown anchors, check-out cooldown anchors,
  open-session markers — each LRU-bounded at `max_tracked_employees`
  and evicted independently; eviction in one map never removes
  entries from another. Accepting a `CHECK_IN` opens/refreshes the
  open-session marker (last writer wins after the check-in cooldown);
  an accepted `CHECK_OUT` closes the marker and anchors the
  independent check-out cooldown (`check_out_cooldown_seconds`),
  never touching the check-in anchor; checkout cooldown rejections
  leave session and anchors untouched; a checkout with no known open
  session creates no event and mutates nothing. No session-capacity
  status was invented; on an evicted marker with an honest hookless
  miss, checkout is rejected as unknown-session.
* **Journal-backed adjudication, fresh per query (S2/S3).** On an
  in-memory session miss, the engine consults an injected
  `open_session_lookup` hook — wired, in durable composition, to
  `make_journal_open_session_lookup(store)`, which `load_events()`s
  the full journal, validates it through the unchanged P2-1
  fail-closed rules, and folds it to per-employee latest events.
  Latest `CHECK_IN` ⇒ open session; `CHECK_OUT` or no events ⇒ none.
  Corruption/I-O/loader errors propagate; they are never converted to
  “no open session”. The lookup is **never cached**: each miss reloads
  the journal.
* **Shared fold, no competing algorithms (S3).** One pure helper,
  `fold_latest_events(events)`, selects per employee the maximum of
  `(occurred_at, journal_position)` — later timestamp wins, equal
  timestamps by the later journal entry, UTC-normalized datetime
  comparison, deterministic. Recovery (check-in anchors over the
  `CHECK_IN` allowlist; session/checkout anchors over all events) and
  the lookup all consume it. Check-in anchor recovery remains exactly
  the pre-P2-6 contract.
* **Single-snapshot startup (S4).** Durable composition calls
  `recover_attendance_startup_state(engine, store, wall_now)`: one
  validated `load_events()` snapshot feeds both the P2-3 check-in
  anchor replay and the P2-6 open-session/check-out-anchor replay
  (byte-compatible with the separate functions, minus the duplicate
  journal read). Recovery completes fully before any attendance
  processing, detector load, or camera open, and session-lookup
  wiring precedes processing; a journal error fails closed before any
  hardware is touched. Default (non-durable) behavior, defaults, CLI
  flags, and the journal's check-in-only production path (no automatic
  checkout trigger) are unchanged.
* **Reporting semantics (S5).** `tools/attendance_report.py --summary`
  counts `check_in` events under `CHECK_INS` and `check_out` events
  under an additive `CHECK_OUTS` table (printed only when the
  selection contains check-outs); check-in-only histories keep the
  prior output. The report is count-oriented: no work duration,
  session window, break time, or payroll inference exists.
* **Schema/rollback boundary (verified from source).** The journal
  stays schema version 1 with the five-key envelope; `check_out`
  records are ordinary version-1 records. Older releases (up to and
  including the P2-5 checkpoint `38984f6`, whose event model lacks
  `CHECK_OUT`) map the unknown `check_out` value to
  `CorruptAttendanceJournalError` in `load_events()` — a whole-journal
  fail-closed rejection, not a per-record skip. Downgrading across
  that boundary to a journal containing checkouts leaves every
  consumer (report CLI exit 2, durable startup, session lookup)
  unable to read the journal; the documented practice is a verified
  backup restore, never hand-editing journal lines.

### D24 — Request IDs: server-generated only, echoed on every response (Phase 5, P5.4.3)

* **Generation.** A pure-ASGI middleware (`src.api_request_context.RequestIDMiddleware`,
  registered once in `create_app`) generates a canonical UUIDv4 from
  `uuid.uuid4()` for every HTTP request, before routing. It is published in
  `scope["state"]["request_id"]` (Starlette's per-request state) and in a
  `ContextVar`, and the `ContextVar` is reset in a `finally` block. There is no
  module-level "current request" object, so concurrent requests cannot overwrite
  or leak one another's ID; Starlette copies the context into its thread pool,
  so synchronous endpoints see it too.
* **Response header.** `X-Request-ID` is written on `http.response.start`, so it
  is present on successes and on every error the application renders. Assignment
  through `MutableHeaders` replaces rather than duplicates an existing value.
* **Caller-supplied IDs are not trusted.** An inbound `X-Request-ID` (or any
  correlation header) is never read or reused, and there is no configuration to
  trust one: letting a client choose the identifier that later appears in audit
  records and incident timelines would allow collisions or planted correlations.
  Proxy headers are not consulted for the same reason.
* **Handler access.** Routes read `request.state.request_id` (via
  `request_id_for(request)`); code without a `Request` uses
  `current_request_id()`.
* **One exception to the middleware path.** Starlette's `ServerErrorMiddleware`
  is outermost, so the catch-all `Exception` handler renders *above* the
  request-ID middleware. That handler therefore attaches the header itself from
  `request.state`; every other handled error passes back through the middleware.
* **Logging.** Refusal and unexpected-error log lines carry the request ID only.
  Request bodies, headers, cookies, authorization values, passwords, hashes,
  session tokens/digests, connection strings and employee/biometric data are
  never logged.
* **Limitations (deliberate).** IDs are per-process evidence, not distributed
  tracing: they are not propagated to other services, not persisted, and the
  log format still carries them as message text rather than a structured field.
  Non-HTTP scopes (lifespan, websocket) receive no ID.

### D25 — Session cookie and cookie-only authentication dependency (Phase 5, P5.4.4)

* **Single cookie source.** `src/api_cookies.py` is the only place a session
  token becomes a cookie. `set_session_cookie` and `clear_session_cookie` read
  every attribute from the approved P5.4.2 configuration (`SESSION_COOKIE`) and
  from `AppSettings.session_cookie_secure`; they define no second policy, no
  `Domain`, and no override. `Lax` is mapped explicitly (header attribute
  values are case-insensitive, RFC 6265 §5.2) rather than lowercased from
  caller text.
* **Attributes.** `va_session`, path `/api/v1`, `HttpOnly`, `SameSite=Lax`,
  `Max-Age=43200`, no `Domain`, `Secure` only when
  `session_cookie_secure` is true (staging/production). The browser lifetime is
  the absolute 12-hour session cap, never the 8-hour idle window: the cookie is
  a maximum age, not a renewal mechanism.
* **Deletion.** `clear_session_cookie` repeats the same name, path, `Secure`,
  `HttpOnly` and `SameSite` (a deletion cookie with different attributes
  addresses a different cookie and leaves the live one behind), sets an empty
  value, `Max-Age=0` and an epoch `Expires`, so removal does not depend on the
  client clock.
* **Credential handling.** The token travels only as the `Set-Cookie` value;
  neither helper accepts or produces a response body, and a malformed token is
  refused (`SessionTokenError`) instead of being written to a browser. Neither
  module logs. The raw token is never persisted: only its digest is stored.
* **Authentication dependency.** `src/api_auth.make_session_dependency(service)`
  returns a FastAPI dependency that reads **only** the `va_session` cookie, then
  calls the existing `SessionService.validate_session` (structural check, digest
  lookup, revocation, absolute cap, idle deadline, account activity). Query
  parameters, bodies, arbitrary headers and client-supplied user IDs/roles are
  never consulted; the identity is the `app_user` row the session points at.
  `AuthenticatedPrincipal` is a frozen dataclass with exactly four fields
  (`user_id`, `username`, `role`, `is_active`) projected from that row by
  `AuthenticatedPrincipal.from_session`.
* **Principal is credential-free (P5.4.4.1).** The principal does not retain the
  `User` or `UserSession` objects at all — not as hidden fields and not behind
  `repr=False`. Dropping the objects, rather than masking them, means neither
  `repr(principal)` nor `fastapi.encoders.jsonable_encoder(principal)` can emit
  `password_hash`, `session_token_hash`, a raw token or any other session
  internal, so the value is safe to return from a route or use in a response
  model. The credential-bearing rows exist only as locals inside
  `SessionService.validate_session` and the dependency call.
* **Failure mapping.** Every authentication outcome — missing, malformed,
  unknown, expired, revoked, inactive or vanished account — raises one
  `AuthenticationRequired`, rendered by `handle_authentication_required` as
  `401 AUTHENTICATION_REQUIRED` with the existing envelope and the P5.4.3
  `X-Request-ID` header. Reasons are not disclosed and the refusal logs only the
  request ID. Storage failures are not caught by the dependency:
  `DatabaseUnavailableError` still becomes `503 DATABASE_UNAVAILABLE`, never a
  401; unexpected failures stay 500.
* **Composition.** `build_session_service(settings)` mirrors
  `build_default_services` — availability is checked before the repository (and
  psycopg) imports, and no connection is opened at construction. `create_app`
  registers the 401 handler; **no route uses the dependency yet**, so the served
  inventory is still the approved eight unauthenticated business endpoints.
  No cookie is issued or refreshed by the dependency, and there is no CSRF,
  throttling, RBAC, audit logging or proxy change (later milestones).

### D26 — Login, password rehash and exact trusted-origin validation (Phase 5, P5.4.5)

* **Endpoint.** `POST /api/v1/auth/login` (`src/api_login.py`) accepts exactly
  `{username, password}` (`extra="forbid"`, username 1-64 chars, password
  1-1024 chars) and returns `200 {"user": {user_id, username, role,
  is_active}}`. Identity comes from the validated `app_user` row; the body never
  contains a token, digest, hash or session record.
* **Verification order** (`LoginService.authenticate`). Lookup through
  `UserRepository.find_by_username` (which derives the key with
  `username_key_for`, so the service never normalises the username itself); one
  real Argon2id verification in every case; then rejection, rehash, or session
  issuance, in that order. Passwords are never trimmed, normalised or logged.
* **Dummy hash.** An unknown account is verified against
  `api_login.DUMMY_PASSWORD_HASH`, a precomputed Argon2id hash (approved
  m=19456/t=2/p=1, 16-byte salt, 32-byte hash) of a random value discarded at
  generation. It is a constant, so no per-request hashing occurs; its plaintext
  is not stored and the value is never returned or logged.
* **Indistinguishable failures.** Unknown username, wrong password and inactive
  account all raise the existing `AuthenticationRequired` → one sanitized
  `401 AUTHENTICATION_REQUIRED`. An inactive account's *real* stored hash is
  verified before refusal, so timing and status reveal nothing. A malformed
  username (too short, control characters) also takes the dummy path and the
  same 401, never a 500.
* **Rehash.** After a successful verification, `PasswordService.needs_rehash`
  triggers `hash_password` and `UserRepository.update_password_hash(user_id,
  password_hash)`, which is `UPDATE app_user SET password_hash = %s, updated_at
  = %s WHERE user_id = %s RETURNING user_id` with `updated_at` from
  `src.clock.utc_now`; only those two columns change and a missing row raises
  `UserNotFound`. Persistence happens **before** session issuance, so a failed
  rehash (e.g. a 503) cannot produce a session; incorrect passwords and
  inactive accounts never rehash.
* **Session and cookie.** `SessionService.issue_session` keeps the approved
  8-hour idle / 12-hour absolute lifetimes and persists only the SHA-256 token
  digest; `set_session_cookie` emits the approved `va_session` cookie
  (`/api/v1`, HttpOnly, Lax, 43200 s, no Domain, Secure in staging/production).
  The raw token exists only as that header value. If issuance fails, the
  response is the existing sanitized 503 with no cookie.
* **Trusted origins.** `TRUSTED_ORIGINS` in `src/config.py` is an immutable
  per-environment mapping of exact defaults: development
  `http://localhost:3000`; testing, staging and production **empty** (login
  unavailable, fail closed). A deployment names its exact dashboard origins
  through `ATTENDANCE_TRUSTED_ORIGINS` (comma-separated); tests inject
  `https://attendance.test.invalid` through the single setting
  `AppSettings.login_trusted_origins`, the same name the login guard reads.
  Every entry — injected or environment-supplied — is canonicalised and
  validated at construction: a wildcard, URL with a path/query, malformed
  value, duplicate or non-tuple raises `ConfigError` instead of being ignored,
  and an unknown `app_env` is refused the same way. There is no wildcard entry
  and no switch that widens or disables Origin validation.
* **Origin validation.** `src/origin.py` parses one strict RFC 6454 origin
  (`http`/`https`, no userinfo, no path/query/fragment, valid host and port,
  canonical lowercasing, default port dropped). A `?` or `#` is refused
  wherever it appears — including a trailing one, which `urlsplit` would
  otherwise report as an empty query/fragment — and scoped IPv6 literals
  (`fe80::1%eth0`) are refused. Login requires **exactly one** `Origin` header
  that equals an allowlist entry; missing, empty, `null`, malformed,
  duplicated, comma-separated and untrusted values all raise
  `OriginNotAllowed` → sanitized `403 ORIGIN_NOT_ALLOWED`. `Host`, `X-Forwarded-Host`,
  `X-Forwarded-Proto` and similar headers are never consulted. An empty
  allowlist fails closed, so staging/production login and logout are
  unavailable until their exact origins are configured.
* **Proxy.** The same-origin Next.js proxy allowlists exactly
  `POST /api/v1/auth/login` and forwards the browser's `Origin` unchanged for
  that route only; an absent Origin is never synthesised, and no other client
  header (cookies, authorization, forwarded host/proto) is passed through.
  The proxy still rebuilds responses from the body, so **the backend's
  `Set-Cookie` is dropped** — browser session establishment is the later BFF
  milestone, and until then login is API-level only.
* **Logout (P5.4.6).** `POST /api/v1/auth/logout` (`src/api_logout.py`) reads
  the raw token **only** from the configured `va_session` cookie (no query or
  body token exists), calls the existing
  `SessionService.revoke_session` — never `revoke_all_for_user` — and answers
  `204 No Content` with the approved deletion cookie from
  `clear_session_cookie` (same name/path/HttpOnly/SameSite/Secure policy, no
  `Domain`, `Max-Age=0`, epoch `Expires`). It is idempotent and never reveals
  session existence: absent/empty cookie, malformed token (rejected
  structurally before any lookup), unknown token (`SessionNotFound`),
  already-revoked and expired sessions all produce the same `204` and the same
  cookie clearing. The existing trusted-Origin dependency guards it, so a
  rejected Origin returns the sanitized 403 **before** any revocation and sets
  no cookie. Only *expected invalid-session* outcomes are treated as
  already-signed-out: a database failure stays the sanitized 503 and an
  unexpected failure the sanitized 500, and in both cases the deletion cookie
  is deliberately **not** sent so the caller keeps it and can retry.
* **Current user (P5.4.7).** `GET /api/v1/auth/me` (`src/api_me.py`) returns
  the session's identity using the existing
  `make_session_dependency`/`AuthenticatedPrincipal` — no second validation
  path, no manual token handling. The body is an explicit `MeResponse`
  (`user_id`, `username`, `role`, `is_active`, `extra="forbid"`) serialized
  with `model_dump(mode="json")` and sent with `Cache-Control: no-store`, so
  credential material cannot be added accidentally and a shared cache cannot
  replay one user's profile to another. Every unusable session (missing, empty,
  malformed, unknown, revoked, expired, inactive account) is the same 401
  `AUTHENTICATION_REQUIRED`; storage failure stays 503 and an unexpected fault
  500. The route is `GET` only and read-only, so it carries no Origin/CSRF
  requirement — login and logout keep theirs unchanged — and no body, query or
  header can select another user.
* **Error mapping.** Login and logout failures keep the existing conventions:
  401 `AUTHENTICATION_REQUIRED`, 403 `ORIGIN_NOT_ALLOWED`, 422
  `REQUEST_INVALID`, 503 `DATABASE_UNAVAILABLE`, 500 `INTERNAL_ERROR` for
  anything unexpected;
  every response carries the server-generated `X-Request-ID`, inbound request
  IDs are ignored, and no password, hash, dummy hash, token, digest or cookie
  is logged.
