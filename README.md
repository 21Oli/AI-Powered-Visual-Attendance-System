# AI-Powered Visual Attendance System

An AI-powered, computer-vision-based attendance system: employees are
enrolled with multiple face samples, and a live camera pipeline detects
faces, assesses their quality, recognizes enrolled people against stored
embeddings, and turns temporally verified recognitions into structured
attendance events.

**Status:** Phase 1 (Computer Vision Core) — Steps 10 (face
recognition), 11 (temporal / multi-frame verification), 12
(unknown-face handling), and 13 (attendance event engine) complete.
Step 14 (testing & evaluation): software regression green (654
tests); empirical CV evaluation BLOCKED on missing model artifacts /
consented labeled data — see
[DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md) and
[reports/phase1_evaluation.md](reports/phase1_evaluation.md). Step 15
(integration demo): integration & automated smoke tests green — run it
with `python tools/run_integration_demo.py --smoke` (deterministic,
hardware-free; `--live` is an explicit opt-in real-camera run). No
real camera/SFace inference was exercised (`models/sface.onnx` still
absent); empirical CV evaluation remains BLOCKED on the Step 14
limitations.

## Core architectural principle

Identity, Recognition, and Attendance are deliberately separate concerns:

```text
Camera → Face Detection → Face Quality → Recognition → Confidence
       → Temporal Verification → Attendance Decision → Attendance Event
```

A face being recognized does **not** automatically mean attendance should
be recorded. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Repository layout

```text
src/          Production code (reusable modules)
notebooks/    Experimentation and demonstration only
data/         Employees, face samples, embeddings, logs (all git-ignored)
models/       Model weights (git-ignored; downloaded at runtime)
tests/        Test suite
reports/      Evaluation reports
docs/         Supplementary documentation
```

> **Notebook = experimentation and demonstration.**
> **`src/` = reusable production code.** Stable logic graduates from
> notebooks into `src/`.

## Setup

Prerequisites: Python 3.14 (Windows x64 was used during development).

```bash
# 1. Create and activate the virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows (cmd/PowerShell)
source .venv/bin/activate       # Linux/macOS

# 2. Install pinned dependencies
python -m pip install -r requirements.txt

# 3. Create your local environment file
cp .env.example .env

# 4. Run the verification suite
.venv/Scripts/python.exe -m pytest tests/ -v    # Windows
python -m pytest tests/ -v                      # Linux/macOS
```

## Face-detection model (manual download)

Step 05's face detector needs the official YuNet ONNX model, which is not
bundled with opencv-python and is intentionally kept out of Git:

```bash
python tools/download_yunet_model.py          # download, verify, install
python tools/download_yunet_model.py --validate-only   # check an existing file
python tools/download_yunet_model.py --help   # all options incl. --replace
```

The application never downloads the model automatically; see
[docs/YUNET_MODEL.md](docs/YUNET_MODEL.md) for provenance, verification,
replacement, and troubleshooting.

## Environment variables

Copy `.env.example` to `.env` and adjust as needed. `.env` is git-ignored;
only keys actually consumed by the implemented code are listed there.

## Project memory

| File | Contents |
|---|---|
| [PROJECT_PLAN.md](PROJECT_PLAN.md) | Roadmap and intended architecture |
| [DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md) | What is actually done / in progress / blocked |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Technical architecture and decisions |
| [CHANGELOG.md](CHANGELOG.md) | Actual implemented changes |

## Git policy

The repository owner is the only person who pushes to GitHub. All agent
work stays local: inspect, implement, verify, and commit locally on request.
