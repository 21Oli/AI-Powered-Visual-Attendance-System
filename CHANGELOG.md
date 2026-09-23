# CHANGELOG.md

All notable changes to the AI-Powered Visual Attendance System.
Contains **actual implemented changes only** — future work is not listed
as done. Format based on Keep a Changelog.

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
