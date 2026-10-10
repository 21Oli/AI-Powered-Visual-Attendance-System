# CHANGELOG.md

All notable changes to the AI-Powered Visual Attendance System.
Contains **actual implemented changes only** — future work is not listed
as done. Format based on Keep a Changelog.

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
