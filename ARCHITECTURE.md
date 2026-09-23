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

### D4 — Dependency licensing note

`insightface`'s library code is MIT, but its pretrained model packages
(buffalo/raccoon families) are licensed for **non-commercial research
only**. This is acceptable for a portfolio/educational project; a
production deployment would require commercially licensed models or
self-trained embeddings. Recorded here so the constraint is visible
before Step 08.
