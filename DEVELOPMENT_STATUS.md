# DEVELOPMENT_STATUS.md

What is actually completed, in progress, blocked, or not started.
Updated after every verified step — never ahead of implementation.

## Step 10 — completion record (2026-10-12)

**Implemented**

* `src/face_recognition.py` — `FaceRecognitionService`: one reusable,
  injectable recognition decision (crop → embedding via the existing
  Step 08 `FaceEmbedder` → eligible stored candidates via the
  existing Step 09 `EmbeddingStorageService` → deterministic cosine
  ranking → explicit typed decision). **Decision only** — no temporal
  verification (Step 11), no liveness, no unknown-face capture, no
  attendance events, no camera, no writes, no network. Recognition is
  a similarity-based decision, never proof of identity.
  * **Similarity:** pure cosine `a·b / (‖a‖‖b‖)` in [-1, 1]
    (`cosine_similarity`, independently unit-testable), computed on
    float64 dot products; validated inputs (1-D, non-empty, finite,
    equal dimensions, non-zero norms) on both query and candidates;
    zero-norm/NaN/infinite inputs raise typed errors instead of
    producing a score; nothing is mutated; no Euclidean distance or
    threshold borrowed from other models.
  * **Decision procedure (deterministic):** rank by
    `(-score, employee_id)`; `NO_CANDIDATES` when none are eligible;
    `UNKNOWN` when best < `min_similarity` (best == threshold ⇒
    MATCHED); `AMBIGUOUS` when `min_margin` is configured and
    `best − runner_up < min_margin` (margin == boundary ⇒ MATCHED;
    identical scores ⇒ margin 0 ⇒ AMBIGUOUS — ties are never resolved
    into identity); otherwise `MATCHED` with the exact employee ID.
    `runner_up`/`margin` reported on every scored result. Single
    candidates have no runner-up and are decided on threshold alone.
  * **`RecognitionConfig` (src/config.py, Step 02 convention):**
    `min_similarity=0.36` (raw-cosine scale) and `min_margin=None`
    (ambiguity checks disabled unless configured) — both
    **explicitly provisional**, not production thresholds; validated
    ranges ([0, 1] / [0, 2]) fail fast; `FaceProcessingConfig.`
    `recognition_threshold` (0.6, a *normalized*-feature value from
    the literature SFace workflow) is deliberately **not reused**
    because Step 08's crop-only mode stores raw embeddings.
  * **Employee eligibility (existing authorities only):** a stored
    record competes only when the Step 07 *published enrollment
    manifest* exists (the "enrollment exists" marker — no second
    source of truth; storage's manifest independence is preserved)
    and passes the active check: trusted from the manifest's recorded
    `is_active` (Step 07 semantics) or overridden by an injectable
    `active_lookup` dependency. Manifests are untrusted input:
    unreadable/malformed/schema-mismatched/identity-contradicting
    manifests raise `ManifestIntegrityError` (fail-closed default;
    `eligibility_error_policy="skip"` is the documented operator
    escape hatch that excludes the affected candidate). Inactive
    employees are simply excluded from a decision — their embeddings
    and manifests are never deleted or modified.
  * **Corruption is never a decision:** corrupt stored records raised
    through explicit candidate requests surface as their typed Step
    09 errors; a vanished record between listing and load raises
    `RecognitionStorageError`; corrupt sidecars on the auto-listed
    path follow Step 09's documented skip-policy (composition is
    tested, never converted into UNKNOWN or a low score).
  * **Typed results:** frozen `RecognitionResult` carrying status,
    `employee_id` (None unless MATCHED — unknown/ambiguous never
    carry identity), `best_score`, `runner_up_score`, `margin`,
    `reason`. **Privacy:** results, errors and logs carry counts,
    IDs, statuses, reasons and scores only — never crops, never full
    vectors; all computation local; no network/camera/telemetry.
  * Construction/import perform no side effects; candidates may be
    narrowed with an explicit `candidate_employee_ids` subset.
* `src/enrollment.py` — added a *backward-compatible* public
  `safe_dir_name(employee_id)` wrapper over the existing private
  encoder, so the recognizer derives the **same** encoded directory
  names as the writer (no scheme duplication, no behavior change).
* `src/config.py` — new frozen `RecognitionConfig` + project-wide
  `RECOGNITION` instance (see above; validated in `__post_init__`).
* `tests/test_face_recognition.py` — 74 deterministic tests on fake
  embedders and temporary-directory-backed real storage (no real
  SFace artifact, no camera, no network, no real biometrics): clear
  match; explicit-candidate subsets; unknown below threshold; exact
  threshold and margin boundaries; ambiguity vs margin-satisfied;
  no-candidates; single candidate; identical-score ties AMBIGUOUS;
  deterministic ranking; dimension mismatch → typed format error;
  empty/NaN/inf/zero-norm candidates and query (corrupt/model-error
  typed); cosine_similarity unit contract (identical/orthogonal/
  opposite non-normalized pairs, dim/zero/NaN/1-D/empty rejections);
  corrupt-record propagation (explicit candidate typed error;
  auto-list composition NO_CANDIDATES); embedder exceptions and
  unusable results → `RecognitionModelError`; vanished-record race →
  `RecognitionStorageError`; config validation incl. inclusive
  boundaries and the documented "provisional" doc note; ID
  preservation (`007`, `EMP-0042`); inactive exclusion/no-candidates,
  embeddings-not-deleted, missing manifest exclusion, malformed/
  contradictory/non-bool manifests → `ManifestIntegrityError`, skip
  policy, `active_lookup` override and its non-bool failure;
  model-compatibility honesty (documented limitation, no provenance
  invention); no mutation of query/candidate/crop/stored arrays;
  no vectors or image data in results/exception messages; import/
  construction purity, `recognize` creating no trees, fresh-
  subprocess and foreign-CWD hygiene, no camera/network surface;
  crop contract rejection before the embedder is ever invoked.

**Verified**

* Focused recognition tests: **74 passed**.
* `python -m pytest` from the project root → **495 passed**, exit 0
  (421 prior + 74 new), stable across 3 repeated full-suite runs.
* External-cwd run (`cd` into an empty temp dir, project venv) →
  **495 passed**, exit 0.
* `git diff --check` → clean.
* Import/construction hygiene (fresh subprocess, foreign CWD):
  importing `src.face_recognition` and constructing the service (with
  never-called injected embedder) performs no camera/model/filesystem
  or network activity; `recognize` with no stored data creates no
  directory trees.

**Model compatibility & real-model status (honest)**

* **Real SFace inference is still NOT verified** — the artifact does
  not exist in the repository and was not downloaded (Steps 08/09
  pending items carried forward). Every recognition test is
  fake-backed with real validation rigor; no accuracy claim is made.
* **No model identifier exists in Step 09 sidecars** (SFace exposes
  no version API at inference time), and none was invented. Dimension
  equality is NOT claimed to prove compatibility: the documented
  remedy is operational — all records compared in one deployment must
  come from one known, consistent embedding-model generation
  (re-embedding after any model change). No metadata format change
  was made (existing records remain byte-compatible, D12 untouched).
* Thresholds are **provisional** starting points on the raw-feature
  cosine scale; empirical calibration against representative
  positive/negative footage is required before any production claim
  (Phase 2). The alternative documented-literature threshold (0.6)
  applies to *normalized* features and was deliberately not adopted.

**Known limitations / deferred**

* Recognition integrates Steps 07–09 through their public APIs only;
  the “one generation” rule is documented, not machine-enforced
  (enforcement would require a sidecar format change, rejected here
  as invasive without operator provenance data).
* Auto-listed corrupt sidecars follow Step 09's skip policy — the
  composition is tested to yield NO_CANDIDATES (never an identity
  decision); deployments requiring fail-closed auto-listing should
  enumerate explicit candidate IDs.
* No unknown-face capture, per-frame streaming, temporal
  confirmation, liveness, or attendance semantics (later steps).

## Step 09 — completion record (2026-10-12)

**Implemented**

* `src/embedding_storage.py` — `EmbeddingStorageService`: saving,
  loading, listing, and validating stored face embeddings under the
  existing configured `AppSettings.embeddings_dir` (`data/embeddings/`,
  already git-ignored). **Storage only** — no matching (Step 10), no
  recognition, no liveness, no attendance events, no enrollment
  changes; storage proves nothing about identity.
  * **Storage format (single choice, no competing formats):** one
    NumPy `.npz` array file per employee (`<encoded-id>.npz`, written
    via `np.savez` and loaded with `np.load(allow_pickle=False)`) plus
    a JSON metadata sidecar (`<encoded-id>.meta.json`, schema
    `format_version: 1` with the exact employee ID, declared
    dimension, dtype marker, and UTC `stored_at`). No `pickle` and no
    object arrays on any path; no database and no new dependency
    (NumPy is already pinned). Raw face images are never stored here,
    and model artifacts stay under `models/`.
  * `<encoded-id>` reuses Step 07's deterministic percent-encoding
    (dots encoded; path separators and `.`/`..` rejected outright), so
    `007` and `EMP-0042` survive byte-exactly and path traversal is
    structurally impossible; IDs must additionally satisfy the exact
    `Employee` model rules (no second identity source-of-truth is
    created and enrollment manifests are never read or modified).
  * **Public API:** `save(employee_id, embedding, *, replace=False)`
    (accepts a Step 08 `FaceEmbedding` or a raw 1-D float array;
    returns the on-disk record re-read and re-validated),
    `load(employee_id)`, `exists(employee_id)`, and
    `list_employee_ids()`; `expected_dimension` constructor contract
    (default `0` = any positive dimension; set e.g. `128` for SFace)
    enforced on **both** save and load so cross-model dimension
    mismatches fail with a typed `EmbeddingRecordFormatError` rather
    than silently comparing across feature spaces.
  * **Validation before save:** ID via `Employee` rules + traversal
    screen; vector 1-D, non-empty, floating dtype, finite, non-zero
    norm (zero-norm and NaN/±inf rejected before any write);
    metadata/dimension consistency. The stored dtype contract is
    float32 (non-float32 inputs are converted; integrity is validated
    the same way on load).
  * **No silent overwrites:** a record exists when *both* files exist;
    `save()` refuses if either exists and `replace=False` (default).
    The explicit `replace=True` flag is the only update path. Under
    concurrency, publication uses hard-linked files when not
    replacing, so the save fails loudly instead of clobbering a
    winner between the existence check and publication.
  * **Persistence safety:** publication order is array-then-sidecar
    (each file is staged, flushed, `fsync`-ed, and then either hard-
    linked or `os.replace`-d into place) so a crash between the two
    publishes can leave an unpublished array but never a declared
    record without validated metadata; on failure only the current
    attempt's staged/unpublished artifacts are cleaned, and
    pre-existing/unrelated files are untouched. Single record file
    pair per employee — no multi-file transactionality is claimed.
  * **Load treats stored files as untrusted input:** records are
    fully re-validated on every load — sidecar schema, exact-ID match
    (a record is never returned under a different employee's ID),
    array file readability via `np.load(allow_pickle=False)`,
    object-array refusal, float32 dtype, 1-D shape, finiteness, and
    metadata-vs-array dimension agreement. Corruption raises typed
    errors (`CorruptEmbeddingRecordError` / `EmbeddingRecordFormatError`),
    never a silent empty vector or a not-found answer. Incomplete
    records (one file without the other) are reported as corrupt —
    `exists()` reports only complete pairs.
  * **Typed errors:** `EmbeddingStorageInputError` (ValueError),
    `DuplicateEmbeddingRecordError`, `EmbeddingRecordNotFoundError`,
    `CorruptEmbeddingRecordError`, `EmbeddingRecordFormatError`,
    `EmbeddingPersistenceError`. Records returned as frozen
    `StoredEmbedding` with a write-protected vector copy.
  * Construction/import performs no filesystem work: directories are
    created only when a real save needs them, and no file is touched
    merely by importing the module or probing `exists`/`list`.
* `tests/test_embedding_storage.py` — 79 deterministic tests on
  temporary directories and synthetic vectors (no real biometrics, no
  network, no camera): round-trip save/load (incl. a Step 08
  `FaceEmbedding` input and float64→float32 normalization), exact-ID
  preservation (`007`, `EMP-0042`, IDs with spaces encoded on disk but
  exact inside the sidecar), vector validation (2-D/empty/int dtype/
  NaN/±inf/zero-norm, expected-dimension on save and load), missing
  records, duplicate refusal (either file present blocks save) and
  explicit `replace` semantics, corruption matrix (garbage `.npz`,
  invalid/non-object JSON, missing `embedding` key, object arrays
  without pickle, a hostile file with smuggled pickle bytes that must
  never execute, NaN-in-file, wrong dtype on disk, array-vs-metadata
  dimension disagreement, sidecar-vs-requested-ID mismatch, zero
  dimension, naive timestamp, unknown dtype marker), incompatible
  schema versions (future major and non-int), path traversal and
  malicious IDs (refused before any write; outside-the-tree sentinel
  untouched), write failures with temp-file cleanup and untouched
  unrelated/pre-existing data, TOCTOU-safe publication refusal,
  hygiene (import/construction produce nothing; foreign-CWD use with
  explicit settings; fresh-subprocess import; no cv2/socket/network
  surface), manifest independence (storage works without any
  enrollment manifest and never touches the enrollment trees), and
  output isolation (write-protected vectors, frozen metadata,
  caller-mutated arrays don't corrupt stored records).

**Verified**

* Focused embedding-storage tests: **79 passed**.
* `python -m pytest` from the project root → **421 passed**, exit 0
  (342 prior + 79 new), stable across 3 repeated full-suite runs.
* External-cwd run (`cd` into an empty temp dir, project venv) →
  **421 passed**, exit 0.
* `git diff --check` → clean.
* Import/construction hygiene (fresh subprocess, foreign CWD):
  importing `src.embedding_storage` and constructing`ness the  service (even probing `list_employee_ids()`) creates no files,
  directories, camera, model, or network activity.
* No biometric artifacts in the repository: all tests used synthetic
  vectors in temporary directories; `data/embeddings/` retains only
  its `.gitkeep`.

**Known limitations / deferred**

* Storage consults **only** the `Employee` model rules for ID form —
  it does not verify that an employee was ever enrolled (no manifest
  is read; avoiding a second identity source-of-truth was a task
  requirement). Recognition-time gating against enrollment existence
  belongs to Step 10.
* Crash between the two publications leaves an orphan array file
  (never a declared record without metadata); such orphans are
  skipped by `list_employee_ids()` and `exists()` but are not
  garbage-collected automatically.
* Two-file publication is not a single atomic transaction
  (documented); the ordering makes the incomplete state detectable
  and typed rather than silent.
* Model identity is not recorded per embedding (SFace has no version
  API surface to read at inference time); dimension compatibility is
  the explicit, configurable contract instead. Vector values are
  normalized to float32 on save.

## Step 08 — completion record (2026-10-11)

**Implemented**

* `src/face_embedding.py` — `FaceEmbedder`: converting one accepted
  face sample (BGR uint8 crop, exactly the format Step 07 persists)
  into a validated float32 1-D embedding. **Generation only** — no
  matching (Step 10), no persistence (Step 09), no liveness or
  attendance.
  * Explicit `load()`/`is_loaded`/`embed()` lifecycle; construction
    and import are side-effect-free;
  * injectable `backend_factory`; without one, the real
    `cv2.FaceRecognizerSF` (SFace, OpenCV Zoo) is used — bundled in
    the pinned `opencv-python` wheel, so **no new dependency** and
    no `onnxruntime` install needed;
  * typed errors: `FaceEmbeddingInputError` (ValueError) for bad
    crops; `FaceEmbeddingModelError` (RuntimeError) for
    missing/invalid artifacts and backend-init failures;
    `FaceEmbeddingOutputError` (RuntimeError) for inference failures
    and non-finite/empty/wrong-shape/zero-norm outputs;
  * frozen `FaceEmbedding` result: write-protected float32 vector,
    `dimension`, pre-normalization `norm`, explicit `VectorDtype`;
  * output validation contract: 1-D, finite, dimension checked
    against the configured `expected_dimension` (0 = accept any
    non-empty 1-D), zero-norm rejected (never divided by zero),
    L2 normalization only when `normalize_output=True`;
  * error messages never include image content or embedding values.
* `tests/test_face_embedding.py` — 33 deterministic tests
  (fake/injected backends, synthetic crops, temporary dirs; **no
  real-model inference anywhere**): lifecycle (construction purity,
  explicit idempotent load, missing-artifact typed error, factory
  failure, non-backend rejection); input contract (None/non-array/
  empty/wrong-ndim/channels/dtype/zero-dim); generation and output
  contract (shape/dtype, 2-D backend output reshape, backend
  receives untouched uint8 3-D, dimension-contract enforcement,
  inference exceptions wrapped, output isolation/write-protection);
  output validation (NaN, ±inf, zero-norm with and without
  normalization, wrong dimensionality, unit-norm after
  normalization, raw-norm preservation); hygiene (import +
  construction create nothing, foreign-WD construction with explicit
  paths, fresh-subprocess check from a foreign CWD); Step 07 format
  compatibility (PNG encode/decode roundtrip → embed succeeds with
  no landmarks or source-frame info) and model-distinction check
  (yunet.onnx ≠ sface.onnx).

**Model approach & evidence**

* **SFace** chosen over insightface buffalo: the standard insightface
  recognition path (`ArcFaceONNX.get` → `face_align.norm_crop`)
  *requires five landmarks* which Step 07 crops do not contain;
  SFace's `FaceRecognizerSF.feature()` performs documented crop →
  112×112 blob preprocessing (scale 1, mean 0, BGR→RGB) that works
  on a plain crop; Apache-2.0 licensing (vs buffalo's non-commercial
  research-only, ARCHITECTURE.md D4); bundled in the pinned
  opencv-python wheel.
* **Crop-only mode is an explicit, honest limitation:** without
  landmark alignment, SFace accuracy is *not* equivalent to the
  documented landmark-aligned workflow. Phase 2 evaluation on real
  footage is required before any accuracy claim.
* The SFace ONNX artifact (`face_recognition_sface_2021dec.onnx`,
  ~38.7 MB, SHA-256 `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79`
  per the upstream Hugging Face model card) is **operator-supplied**
  at `models/sface.onnx` (`DEFAULT_MODEL_PATH`) and **never
  downloaded** by this codebase. Until provided, `load()` raises
  `FaceEmbeddingModelError`; unit tests run exclusively on fakes.

**Verified**

* Focused embedding tests: **33 passed**.
* `python -m pytest` from the project root → **342 passed**, exit 0
  (309 prior + 33 new), stable across 3 repeated full-suite runs.
* External-cwd run (`cd` into an empty temp dir, project venv) →
  **342 passed**, exit 0.
* `git diff --check` → clean.
* Import/construction hygiene (fresh-subprocess, foreign CWD):
  importing `src.face_embedding` and constructing `FaceEmbedder`
  open no camera, load no model, touch no network, create no
  directories.

**Known limitations / deferred**

* **Real-model inference is pending** — no SFace artifact exists in
  the repository and none was downloaded in this step; all embedding
  tests use injected fakes with the same validation rigor, and no
  test claims real face-recognition accuracy.
* Crop-only (no landmark alignment) embedding quality is unmeasured;
  aligning YuNet's 5-point detections would require re-running
  detection at embed time (Step 07 crops carry no source frame or
  landmarks by design).
* `expected_dimension=0` default accepts any non-empty 1-D vector;
  set it to 128 for real SFace usage to enforce the documented
  128-D contract.

## Step 07 — completion record (2026-10-11)

**Implemented**

* `src/enrollment.py` — `EnrollmentService`: reusable multi-image face
  enrollment orchestration between identity (Step 03), camera (Step
  04), detection (Step 05) and quality (Step 06). It contains no
  embeddings (Step 08), no recognition (Step 10), no liveness, no
  attendance logic, and no GUI. Camera, detector, assessor, enrollment
  config, and `AppSettings` are all injectable — tests run fully
  hardware-free.
  * `enroll(employee_id, full_name, *, is_active=True, should_cancel=None)`
    runs one bounded capture session: validate identity → refuse
    duplicates → open the camera → read frames → detect → apply the
    existing face-count policy and per-face assessor → collect valid
    samples → persist. Returns a frozen `EnrollmentResult` with an
    explicit `EnrollmentStatus` (COMPLETED / CANCELLED / FAILED — never
    a bare bool), accepted `SampleRecord`s (filename, frame index,
    UTC capture time, measured `FaceQuality`, published path), every
    non-accepting attempt as a `SampleRejection` (reason + quality),
    the total frame attempt count, exact-duplicate skip count, and an
    error string (no image data) for FAILED/CANCELLED sessions.
  * Terminations are bounded and explicit: `min_samples` reached →
    COMPLETED; caller `should_cancel()` (polled before every attempt) →
    CANCELLED with nothing persisted, even if the minimum was already
    collected mid-session; `EnrollmentConfig.max_capture_attempts`
    exhausted → FAILED. Camera read failures and rejections count as
    attempts (bounded, never endless); camera acquisition/open errors,
    detector/assessor exceptions, and persistence failures are fatal
    session errors, not sample rejections. Camera release happens on
    every exit path (success, cancellation, failure, exception).
  * Employee validation reuses the `Employee` model exactly: IDs keep
    leading zeros, hyphens, case (`007`, `EMP-0042`); empty/whitespace
    IDs/names rejected; IDs with path separators or equal to `.`/`..`
    rejected before any capture. Active-employee semantics preserved
    (recognition gating of inactive employees is a later step).
  * Persistence: `<data>/employees/<encoded-id>/employee.json` (schema
    `format_version: 1`, full Employee serialization, sample count,
    per-sample file/UTC time/quality metrics) and
    `<data>/face_samples/<encoded-id>/sample_001.png …`. `<encoded-id>`
    is a deterministic, reversible, collision-free percent-encoding
    (dots included), making path traversal structurally impossible
    while preserving the exact ID in the manifest. An existing
    enrollment is never overwritten (checked before capture and again
    before staging). Samples are staged in hidden `.staging-`
    directories, PNG-coded and decoded for verification, and published
    samples-first, manifest-last (`os.replace`) — the manifest is the
    "enrollment exists" marker, so a crash between publishes leaves
    orphan samples without a recognized enrollment. On failure only
    current-attempt artifacts are cleaned; pre-existing/unrelated data
    is preserved. Atomicity across the two trees is best-effort, not
    transactional (documented).
  * Exact-duplicate suppression: accepted crops are sha-256 hashed; a
    byte-identical crop is skipped (reported in
    `duplicates_skipped`, not silently hidden). This is frame-level
    hygiene, not a biometric similarity system — no embeddings at this
    step, duplicate/near-duplicate filtering beyond byte-equality is
    deferred to later steps.
* `src/config.py` — `EnrollmentConfig` gains `max_capture_attempts=60`
  (hard cap on frame attempts per session; validated
  `>= min_samples`). Sample-count constants unchanged.
* `tests/test_enrollment.py` — 38 deterministic tests with fake
  camera/detector/assessor doubles and synthetic NumPy frames in
  temporary directories (no webcam, no network, no model file, no real
  biometric images): valid input; empty/whitespace IDs and names;
  leading zeros and hyphens preserved; path-traversal IDs refused;
  duplicates refused byte-for-byte untouched; capture continues past
  no-face/multi-face/quality-rejected frames with preserved reasons;
  completion at `min_samples`; cancellation before/after success
  publishes nothing; attempt budget enforced; camera open/factory/read
  failures; camera released on every path; detector/assessor exceptions
  fatal; byte-identical crops skipped and reported; persistence layout,
  naming, and manifest schema; encoding of IDs with special characters;
  persistence failure cleanup with no staging remnants and untouched
  pre-existing/unrelated data; import/construction hygiene (no
  filesystem side effects).

**Verified**

* Focused enrollment tests: **38 passed**.
* `python -m pytest` from the project root → **309 passed**, exit 0
  (271 prior + 38 new), stable across 3 repeated full-suite runs.
* External-cwd run (`cd` into an empty temp dir, project venv) →
  **309 passed**, exit 0.
* `git diff --check` → clean.
* Fresh-interpreter + foreign-CWD checks: `import src.enrollment` and
  `EnrollmentService(settings=...)` construction perform no filesystem
  side effects; the default (unloaded) `FaceDetector` raises a typed
  `FaceDetectorError` on first use — the YuNet model is never
  auto-downloaded and never required by tests.

**Known limitations / deferred**

* Sample-count thresholds and capture budget are initial, testable
  defaults; calibration on real footage is Phase 2 work.
* Near-duplicate biometric similarity is a non-goal at this step
  (embeddings, Step 08); only exact byte-duplicate suppression exists.
* Multi-file persistence is not transactional: a crash between sample
  and manifest publication leaves orphan sample files (never a declared
  enrollment without images); manual cleanup would be required.
* No CLI/GUI for enrollment sessions; interactive capture UX is a
  later phase.

## Step 06 — completion record (2026-10-10)

**Implemented**

* `src/quality.py` — `FaceQualityAssessor`: reusable, deterministic
  face-quality assessment between detection (Step 05) and enrollment
  (Step 07). Pure image metrics — no camera, no model load, no
  filesystem, no network, no enrollment;
  * `assess_face(frame, detection)` — per-face **image quality**:
    box size (usable/camped area), Laplacian-variance sharpness,
    grayscale mean brightness, and an optional landmark-based
    orientation proxy. Returns a frozen `FaceQuality` result with an
    explicit `FaceQualityDecision` (ACCEPT/REJECT — never a bare bool)
    and a stable machine-readable `FaceQualityReason` naming the
    first-failed check (order: OUT_OF_FRAME → TOO_SMALL →
    NO_LANDMARKS/POSE → BLUR → TOO_DARK → TOO_BRIGHT);
  * `assess_frame_faces(detections)` — **face-count policy** separate
    from image quality: `NO_FACES` (empty frame), `MULTIPLE_FACES`
    (single-face policy on), or `None` (policy satisfied). The face
    count never merges with per-face quality: in a multi-face frame
    every face is still individually assessable;
  * invalid caller inputs (None/wrong-shape/wrong-dtype/empty frames,
    zero-area boxes, non-sequence detections) raise
    `FaceQualityInputError` (a `ValueError`) — `FaceQualityReasons`
    are reserved for *valid* inputs that fail the quality gates.
    Out-of-frame/mostly-outside faces are documented returns, not
    exceptions.
* `src/config.py` — new frozen `QualityConfig` + project-wide `QUALITY`
  instance: `min_face_size_px=80`, `min_sharpness=50.0`,
  `min_brightness=40`, `max_brightness=220`, `pose_yaw_tolerance=0.15`,
  `pose_pitch_tolerance=0.35`, `pose_policy` (`"optional"`/
  `"required"`), `require_single_face=True`. Fail-fast `ValueError`
  validation; thresholds are initial calibration starting points, not
  claims of production biometric accuracy.
* `src/face_detection.py` — **backward-compatible** `FaceDetection`
  extension: optional `landmarks: tuple[(x, y), ...] | None` (5 points,
  canonical YuNet order — right eye, left eye, nose tip, right mouth
  corner, left mouth corner). Populated from real backend rows in the
  full 15-column format; `None` for legacy 5-column rows; never
  synthesized; non-finite landmark values drop the landmarks but keep
  the box. Existing construction sites work unchanged.
* `tests/test_quality.py` — 56 deterministic tests (synthetic
  NumPy/OpenCV images, real `cv2` metric computation; private-seeded
  RNG so order-independent expected values; zero-area/negative-origin
  boxes probed via `object.__new__` doubles) covering: acceptance,
  each rejection reason, stable reason values, first-failed-check
  order, box clamping (partial, mostly-out, fully-out, edge-touching),
  frozen results, input-frame/detection immutability, face-count
  policy (0/1/2+ faces, policy off), pose frontal/yaw/pitch,
  `pose_required` / landmarks-absent behavior, threshold flips,
  config validation, scope guarantees (no file writes, no camera or
  network surface) and fresh-process import hygiene.

**Pose support & limitations (documented, honest)**

* The pose check is a **landmark geometric proxy** — nose-tip offset
  from the eye midpoint, normalized by box width (yaw-ish) and height
  (pitch-ish). It is **NOT 3D pose estimation** and cannot distinguish
  head rotation from facial-shape variation.
* Default `pose_policy="optional"`: without landmarks the check is
  skipped and the metrics stay `None` — never invented.
  `pose_policy="required"` rejects such detections with
  `NO_LANDMARKS`.
* Real calibration (are 0.15/0.35 right?) is deferred to Phase 2
  evaluation on real enrollment footage.

**Verified**

* Focused quality tests: **56 passed**.
* Landmark regression tests in `tests/test_face_detection.py`:
  **4 passed** (file total 46).
* `python -m pytest` from the project root → **271 passed**, exit 0
  (215 prior + 56 new), stable across repeated runs (order-sensitivity
  root-caused and fixed via a test-private RNG seed).
* External-cwd run (`cd` into a temp dir, project venv) →
  **271 passed**, exit 0.
* `git diff --check` → clean.
* Import/cli checks: `import src.quality` + `import src.config` succeed
  in a fresh interpreter; fresh-process hygiene tests confirm no
  camera/`FaceDetectorYN_create` activity at import or assessor
  construction and no unrelated ML stack pulled in.

**Known limitations / deferred**

* Thresholds (80 px, 50.0 Laplacian variance, brightness 40–220,
  pose 0.15/0.35) are initial testable defaults; real-accuracy
  calibration on target footage is Phase 2 work.
* The pose proxy inherits detector-landmark accuracy; no alternative
  landmark source, no eye-openness/mouth-occlusion metrics, no
  liveness, no multi-frame decisioning — all later steps by design.
* `assess_frame_faces` takes only the detection list (not the frame);
  merging frame-level context into quality scoring is deliberately
  not done in this step.

## Phase 1 — Computer Vision Core

| # | Step | Status | Notes |
|---|------|--------|-------|
| 01 | Project structure & environment | ✅ Complete | Verified 2026-09-23 — 17/17 tests pass. Details below. |
| 02 | Configuration & constants | ✅ Complete | Verified 2026-09-23 — 35/35 tests pass (18 new config tests). Details below. |
| 03 | Employee data model | ✅ Complete | Verified 2026-10-09 — 71/71 tests pass (36 new employee tests). Details below. |
| 04 | Live camera service | ✅ Complete | Verified 2026-10-09 — 101/101 tests pass (30 new camera tests). Details below. |
| 05 | Face detection | ✅ Complete | Verified 2026-10-09 — 143/143 tests pass (42 new face-detection tests); YuNet backend requires an operator-supplied ONNX model. Details below. |
| 06 | Face quality assessment | ✅ Complete | Verified 2026-10-10 — 271/271 tests pass (56 new quality tests + 4 landmark regression tests). Details below. |
| 07 | Multi-image face enrollment | ✅ Complete | Verified 2026-10-11 — 309/309 tests pass (38 new enrollment tests); new `max_capture_attempts` constant in `EnrollmentConfig`. Details below. |
| 08 | Face embedding generation | ✅ Complete | Verified 2026-10-11 — 342/342 tests pass (33 new embedding tests); SFace backend via injectable `FaceEmbedder`, real model artifact pending. Details below. |
| 09 | Embedding storage | ⬜ Not started | |
| 10 | Face recognition | ⬜ Not started | |
| 11 | Temporal / multi-frame verification | ⬜ Not started | |
| 12 | Unknown-face handling | ⬜ Not started | |
| 13 | Attendance event engine | ⬜ Not started | |
| 14 | Testing & evaluation | ⬜ Not started | |
| 15 | Phase 1 integration demo | ⬜ Not started | |

## Phases 2–7

Not started (intentionally blocked until Phase 1 is stable):

Attendance/business rules · PostgreSQL + FastAPI · Web dashboard ·
Security/RBAC/audit · Docker/deployment · Advanced liveness + optimization.

## Step 01 — completion record

**Implemented**

* `.gitignore` (venv, secrets, biometric data, logs, model weights)
* Python 3.14.7 venv at `.venv/`; `requirements.txt` pinned to the exact
  installed versions (numpy 2.5.3, opencv-python 5.0.0.93,
  python-dotenv 1.2.3, pytest 9.1.1)
* Directory skeleton per `PROJECT_PLAN.md`
  (`src/ notebooks/ data/* models/ tests/ reports/ docs/`)
* `src/logging_setup.py` — `get_logger()` with console + rotating-file
  handlers (idempotent), configured via `LOG_LEVEL` / `LOG_DIR`
* `.env.example` (only keys consumed by implemented code)
* `tests/test_environment.py` — 17 verification tests
* Project memory docs: README, PROJECT_PLAN, ARCHITECTURE, DEVELOPMENT_STATUS, CHANGELOG
* `pytest.ini` (`pythonpath = .` so both `pytest` and `python -m pytest` work)

**Verified**

* `pytest tests/ -v` → **17 passed** (venv interpreter, Python 3.14)
* Bare `.venv/Scripts/pytest.exe` → **17 passed**
* External-cwd run → **17 passed** (path-independence)
* `git check-ignore` confirms `.venv/`, `data/logs/`, `models/*.zip` excluded
* Wheel installs for all four packages succeeded on Python 3.14 with no fallback needed

**Known limitations / deferred**

* `notebooks/` is empty — notebooks are created by their roadmap steps.
* No application functionality yet (per plan): camera, detection,
  recognition, embeddings, enrollment, attendance are all later steps.

## Step 02 — completion record

**Implemented**

* `src/config.py` — centralized configuration in two layers:
  * frozen application constants: `APP_NAME`, `PROJECT_ROOT` (derived
    from `__file__` — working-directory independent), `CAMERA`,
    `FACE_PROCESSING`, `ENROLLMENT` (validated frozen dataclasses)
  * env-backed `AppSettings` via `load_settings()`: `APP_ENV`, `DEBUG`,
    `DATA_DIR`, `LOG_DIR` plus derived paths (employees, face_samples,
    embeddings, models, reports)
* `ConfigError(ValueError)` fail-fast handling for invalid environment
  values; invalid constants raise `ValueError` at import
* `ensure_directories(settings)` — explicit directory creation only;
  importing the module performs no side effects
* `.env.example` updated: `APP_ENV` and `DEBUG` added, env-vs-constants
  split documented
* `tests/test_config.py` — 18 tests mapped to the six Step 02 requirements

**Verified**

* `python -m pytest` → **35 passed** (17 Step 01 + 18 Step 02)
* External-cwd run → **35 passed**
* Foreign-cwd subprocess test proves path resolution is cwd-independent
* Fresh-interpreter check: importing `src.config` imports no CV/ML stack
  and creates no directories

**Configuration decisions**

* Environment variables only for environment-specific values (`APP_ENV`,
  `DEBUG`, `DATA_DIR`, `LOG_DIR`); camera/face/enrollment constants live
  in `src/config.py` (ARCHITECTURE.md D5)
* Frozen dataclasses everywhere — no global mutable state
* Fail-fast validation at import/load time
* Step 01 logging contract unchanged (`LOG_LEVEL`/`LOG_DIR` still honored)

## Step 03 — completion record

**Implemented**

* `src/employee.py` — the identity layer: `Employee` frozen/slots dataclass
  with `employee_id`, `full_name`, `is_active`, `created_at` and
  `__post_init__` validation raising `EmployeeValidationError(ValueError)`
  with field-specific messages; explicit `to_dict()` serialization
* `tests/test_employee.py` — 36 tests mapped to the nine Step 03
  requirements
* New `ARCHITECTURE.md` decision **D6**: identity boundaries (no biometric
  fields, no CV/ML/database dependencies in the employee model)

**Model design & validation rules**

* `employee_id` — non-empty after stripping surrounding whitespace;
  every other character preserved exactly (leading zeros, hyphens,
  underscores, mixed case all valid — real business IDs like `"007"` or
  `"EMP-0042"` survive unchanged; no case-folding). Uniqueness is a
  storage-layer concern, not a model-construct concern.
* `full_name` — non-empty after stripping plus collapsing internal
  whitespace runs to a single space (documented normalization); case
  preserved as provided.
* `is_active` — must be a real `bool`; strings/ints/None rejected,
  never coerced.
* `created_at` — defaults to `datetime.now(UTC)`; custom values must be
  timezone-aware datetimes (naive rejected); non-UTC zones accepted.
* Frozen: mutation raises `FrozenInstanceError`; slots for compactness.
* No speculative fields added beyond the four required identity fields.

**Verified**

* `python -m pytest` → **71 passed** (35 Step 02 + 36 Step 03) from the
  project root
* External-cwd run (`cd /tmp`, project venv specified explicitly) →
  **71 passed** — import guarantees hold regardless of working directory
* Focused employee tests: **36 passed**
* `git diff --check` → no whitespace errors
* Fresh-interpreter check: importing `src.employee` imports no CV/ML
  stack (no cv2, mediapipe, insightface, onnxruntime, numpy) and performs
  no side effects

**Known limitations / deferred**

* No employee persistence/repository yet — that is squarely Step 09
  (embeddings/employee storage) plus Phase 3 (PostgreSQL); `to_dict()`
  is serialization for logging/tests/interchange only.
* Uniqueness of `employee_id` is not checked by the model (needs a
  storage session; enforced in later steps).

## Step 05 — completion record

**Implemented**

* `src/face_detection.py` — `FaceDetector` built on `cv2.FaceDetectorYN`
  (YuNet ONNX, already pinned via opencv-python; **no new dependency**).
  Explicit `load()` / `is_loaded` / `detect()` lifecycle.
* `FaceDetection` — frozen, slotted result dataclass with
  `x, y, width, height` (pixel coordinates, top-left origin) and an
  optional backend-supplied `confidence` (None when absent; never
  invented).
* Frame contract: BGR uint8 `(H, W, 3)`; validation raises
  `FaceDetectionInputError` (a `FaceDetectionError` and `ValueError`)
  for None, empty, wrong dimensionality, unsupported channels,
  zero-size dimensions, and non-uint8 dtypes. Input frames are never
  mutated or copied.
* Errors: `FaceDetectorLoadError` (missing/corrupt model or backend
  init failure — directories are never auto-created, models never
  auto-downloaded), `FaceDetectorError` (not loaded or backend
  failure during detection — never silently misreported as
  "no faces").
* `tests/test_face_detection.py` — 42 deterministic tests using typed
  `StubBackend` doubles plus real-backend load-failure integration and
  fresh-process import-hygiene checks (no camera, no model load, no
  network, no filesystem writes at import).

**Detected backend and model lifecycle**

* opencv-python 5.0.0.93 was inspected empirically: it ships **no**
  cascade XMLs and no YuNet model file (verified: empty `cv2/data/`,
  no `CascadeClassifier` symbols). Any cascade-based plan would have
  required downloading files at runtime — not acceptable per the task,
  so `FaceDetectorYN` with an operator-supplied YuNet ONNX model was
  chosen instead.
* Model file expected at `<project>/models/yunet.onnx`
  (`DEFAULT_MODEL_PATH`); the operator downloads it from the official
  OpenCV zoo. `load()` fails with `FaceDetectorLoadError` and a clear
  message if it is missing or invalid; import is untouched.
* `confidence` uses the configured score threshold
  (`FACE_PROCESSING.detection_confidence = 0.5` from Step 02); no
  detection parameters are hardcoded in the module.

**Verified**

* Focused face-detection tests: **42 passed**
* `python -m pytest` from project root → **143 passed**, exit 0
  (101 prior + 42 new)
* External-cwd run (`cd /tmp`, project venv) → **143 passed**, exit 0
* `git diff --check` → clean

**Known limitations / deferred**

* No YuNet model file exists in the repo (per design); real-image
  detection accuracy requires the operator to provide
  `models/yunet.onnx` and manually verify on target hardware. Test
  coverage exercises the real backend only on failure paths (missing/
  corrupt model) and a no-face synthetic control image.
* Detection confidence/box quality on real faces, NMS tuning, and
  performance are deferred until Step 06/07 consume real frames.

## Step 04 — completion record

**Implemented**

* `src/camera.py` — `CameraService` wrapping `cv2.VideoCapture`:
  open (`CameraOpenError` on failure), `read_frame()` (`(False, None)`
  on failure, never a fabricated frame), idempotent `release()`,
  accurate `is_open`, context-manager support (`__enter__` opens,
  `__exit__` always releases even on exception), and strict lifecycle
  errors (`CameraStateError`) for invalid-state operations
  (read/open misuse). Error hierarchy: `CameraError` → `CameraOpenError`
  / `CameraStateError`.
* `tests/test_camera.py` — 30 hardware-independent tests using
  deterministic `FakeCapture` / `FakeCaptureFactory` doubles for
  `cv2.VideoCapture` (mock injection at `src.camera.cv2.VideoCapture`),
  mapped to the thirteen Step 04 behaviors plus import hygiene.

**Resource-management & error-handling decisions**

* Failed opens release the half-initialized handle and leave
  `_capture = None` — no leaked device handle; retry-after-failure works.
* Property application failures (width/height not honored by the
  device) also release and raise `CameraOpenError`.
* `release()` swallows its own failures (logged via the Step 01 logging
  contract) so cleanup never masks an in-flight exception; returning
  `None` from `__exit__` lets exceptions propagate unchanged.
* `_safe_release()` is static — no capture mutation during cleanup.
* Read failures (`read()` returns `False`/`None`, or empty array) return
  `(False, None)` — never a stale or fabricated frame.
* Configuration comes from `src.config.CAMERA` only — a test asserts no
  hardcoded index/dimensions in the module's source.

**Verified**

* Focused camera tests: **30 passed**
* `python -m pytest` from project root → **101 passed** (71 prior +
  30 new), exit 0
* External-cwd run (`cd /tmp`, project venv) → **101 passed**
* Import hygiene (fresh interpreter): importing `src.camera` does not
  instantiate `cv2.VideoCapture` (verified against a patched capture
  class) and imports no ML stack

**Known limitations / deferred**

* No automatic retry or reconnection, no background streaming thread,
  no multi-camera management, no frame-rate control beyond advisory
  `CAMERA.target_fps` — all deferred by design.
* Physical-camera behavior (real device property negotiation, actual
  frame timing) can only be sanity-checked manually on target hardware;
  the suite is intentionally hardware-independent.
