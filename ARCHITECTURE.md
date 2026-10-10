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

### D4 — Dependency licensing note

`insightface`'s library code is MIT, but its pretrained model packages
(buffalo/raccoon families) are licensed for **non-commercial research
only**. This is acceptable for a portfolio/educational project; a
production deployment would require commercially licensed models or
self-trained embeddings. Recorded here so the constraint is visible
before Step 08.
