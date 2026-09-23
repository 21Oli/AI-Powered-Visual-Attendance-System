# CHANGELOG.md

All notable changes to the AI-Powered Visual Attendance System.
Contains **actual implemented changes only** — future work is not listed
as done. Format based on Keep a Changelog.

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
