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
hardware-free; `--live` is an explicit opt-in real-camera run). The
operator-supplied `models/yunet.onnx` and `models/sface.onnx` enable
`--live`'s full pipeline path; real-camera recognition results have
not yet been recorded, and empirical CV accuracy evaluation remains
BLOCKED on consented labeled data (see
[reports/phase1_evaluation.md](reports/phase1_evaluation.md)).

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
data/         Employees, face samples, embeddings, attendance journal,
              logs (all git-ignored)
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

## Operator enrollment (consent required)

Make one employee a recognition candidate by capturing face samples and
embedding them into the single stored embedding the recognizer discovers:

```bash
python tools/enroll_employee.py \
    --employee-id "EMP-0042" --full-name "Ada Lovelace" --confirm-consent
```

* **Prerequisites:** `models/yunet.onnx` and `models/sface.onnx` must be
  present (see above / the live demo's notes); the tool aborts with a
  clear message before touching the camera if either is missing.
* **Consent is mandatory:** pass `--confirm-consent` only after the
  participant has explicitly agreed to face capture, sample storage, and
  embedding creation. Without the flag the tool does nothing (exit 2).
* **What happens:** the existing enrollment service captures and
  publishes face samples (`data/face_samples/…`, `data/employees/…/employee.json`),
  then the existing bridge embeds the published samples into exactly one
  stored embedding (`data/embeddings/…`). The tool finishes only after it
  can confirm the ID is discoverable through
  `EmbeddingStorageService.list_employee_ids()` — the same candidate list
  the live recognizer uses.
* **Camera frame size:** the tool requests the project's configured
  default camera resolution (`src.config.CAMERA`: 1280×720). If the
  device does not deliver that resolution, pass an explicit
  `--frame-size WIDTHxHEIGHT` override (e.g. `--frame-size 640x480`,
  same parser as the live demo's `--frame-size`). Validation is never
  bypassed: dimensions must be positive and the camera must actually
  apply the requested size, or acquisition fails loudly as before.
* **Duplicates are never overwritten.** An existing enrollment or an
  existing stored embedding aborts the run (exit 1) before anything is
  changed. Re-enrollment for the same ID requires manually deleting that
  employee's data (`data/employees/<encoded-id>`, `data/face_samples/<encoded-id>`,
  `data/embeddings/<encoded>.*`) first — an operator-owned, explicit action.
* **Partial failure & recovery:** if capture fails, nothing is published;
  if embedding fails after a completed capture, the published enrollment
  stays on disk (it is legitimate Step 07 data) and the employee simply
  is not a recognition candidate until embedding succeeds — fix the
  cause and re-run, or delete the enrollment data directories manually
  if you want a clean re-enrollment. Face crops, samples, and embeddings
  are never printed or logged.
* **Verify without any camera run:**
  `python tools/run_integration_demo.py --smoke` (deterministic,
  hardware-free) or, opt-in, `python tools/run_integration_demo.py --live`
  with the participant in view — `recognition: matched` replaces
  `no_candidates` now that a candidate exists.
* **Sharpness diagnostic (opt-in, console-only):** during a `--live`
  run, `--report-quality-metrics` makes frames rejected specifically
  for `blur` also print the measured sharpness value and the
  configured threshold (numbers only; nothing saved to files or
  logs). Disabled by default. Use the measured values to inform the
  Phase-2 threshold-calibration discussion — they never auto-tune
  anything.
* **Unknown-face handling (Step 12, live):** during a `--live` run the
  real `UnknownFaceHandler` now consumes every full-chain temporal
  result and prints report-only console lines — `unknown-face outcome
  observed (…)` for the first unresolved face in an episode,
  `suppressed (…; suppressed_frames=N)` for repeats inside the
  30-second cooldown, and `episode_ended (suppressed_frames=N)` when a
  temporally verified identity resolves the episode. Console lines
  only: never crops, embeddings, or identities, and nothing is saved
  to files or logs by this reporting. A single MATCHED frame is still
  never treated as verified (the Step 11 streak requirement stands).

The `--live` demo itself never enrolls; enrollment is operator-driven and
consent-gated by design.

> **Live status (2026-10-20, log-backed):** two real `--live` sessions
> ran on the development machine — the operator-supplied `yunet.onnx`
> and `sface.onnx` loaded, the real camera acquired (index 0) and
> released cleanly, with zero errors in `data/logs/app.log`. These
> sessions performed **no enrollment**, so no recognition decision was
> possible; real-camera recognition accuracy remains unrecorded. The
> remaining open rehearsal step is a supervised consented
> enroll → recognize (`VERIFIED` event) → delete cycle.

### Deleting an enrollment (receipt-required, irreversible)

Remove one employee's samples, manifest, and stored embedding when they
withdraw consent or the record is wrong:

```bash
# 1. Preview — prints exactly which artifacts exist for the ID; no changes:
python tools/delete_enrollment.py --employee-id "EMP-0042"

# 2. Delete — requires TWO separate flags; neither bypasses validation:
python tools/delete_enrollment.py --employee-id "EMP-0042" --execute --confirm-delete
```

* **What is deleted:** only the selected employee's own artifacts —
  `data/embeddings/<encoded>.npz` + `<encoded>.meta.json`,
  `data/employees/<encoded>/` (the `employee.json` manifest), and
  `data/face_samples/<encoded>/`. Shared model files, configuration,
  logs, and other employees' records are never touched. All paths are
  derived from the validated ID (path traversal and invalid IDs are
  rejected before anything runs).
* **Confirmation:** `--execute` alone is refused (exit 2). Both flags
  together trigger deletion; they never bypass the ID validation or the
  target-existence check (deleting a nonexistent enrollment is refused,
  exit 1).
* **Order & crash behavior:** deletion mirrors enrollment's publish
  order in reverse (embedding record first, manifest before samples),
  so a mid-deletion crash can only leave the publish-safe state
  (orphaned samples without a declared enrollment) — never a declared
  enrollment without its images. A failure prints exactly what was
  removed and what remains; success is printed only after the
  employee's ID is provably absent from
  `EmbeddingStorageService.list_employee_ids()`.
* **Recovery limitation:** deletion is irreversible — deleted
  biometric artifacts are recoverable only by re-enrolling the
  (re-consenting) participant.

## Attendance event storage

Accepted attendance events are durably journalled as JSON Lines at
`data/attendance/events.jsonl` (`AttendanceEventStore`). A composition
service (`DurableAttendanceService`) feeds engine decisions to the
store and reports exactly four outcomes — no event, durably persisted,
persistence rejected before any write (confirmed failure), or
persistence unconfirmed (original error preserved; no automatic
retry). The integration demo wires this composition into **opt-in**
live and smoke paths behind `--durable` (default off — see below);
without that flag no journal is loaded or written anywhere. One record
per event carries exactly `schema_version`, `event_id`, `employee_id`,
`event_type`, and `occurred_at` (UTC) — never images, embeddings,
scores, or unknown-person data. Appends use flush+fsync per record (a failed write
is reported as an unconfirmed record); an interrupted append can leave
an incomplete trailing line, which loading excludes *and reports*
(`incomplete_trailing_record=True`) while corruption anywhere else
raises a typed error with the exact line number. Details:
[DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md) Steps 21–25.

### Inspecting the journal (read-only report CLI)

A on-demand, read-only operator utility inspects the journal:
`python tools/attendance_report.py` lists events (chronological by
default, `--journal-order` for append order), filterable by verbatim
employee ID (`--employee`, repeatable) and by UTC date half-open
intervals (`--since`/`--until`/`--day`), with `--summary` for
per-employee × per-day check-in counts. It never appends, repairs, or
rewrites the journal; it is **not wired into the live attendance
workflow** (the live demo remains unconnected to persistence). Exit
codes: `0` success (including an empty journal and a torn trailing
record — the torn tail is reported on stderr while valid rows print),
`2` corrupt/unsupported journal data (typed error, no rows printed),
`3` invalid arguments or dates. Site-local timezone display/filtering
is intentionally not supported yet — the journal stores UTC only.

### Durable live/smoke runs (opt-in, `--durable`)

One opt-in flag wires the journal into the integration demo for both
modes; without it, nothing is loaded or written and behavior is exactly
as before:

```bash
python tools/run_integration_demo.py --smoke --durable   # journal inside the run's temporary directory
python tools/run_integration_demo.py --live --durable    # journals to data/attendance/events.jsonl
```

* **What is persisted:** only check-in events the real attendance
  engine authorizes (verified identity + cooldown eligibility) — one
  record, the exact `schema_version`/`event_id`/`employee_id`/
  `event_type`/`occurred_at` (UTC) envelope, appended with per-record
  `fsync`. Repeat check-ins are suppressed by recovering previously
  journaled cooldown anchors *before* any event is processed.
* **Failure behavior (fail closed):** corrupt or unreadable journal
  data stops durable mode before any event is processed (smoke: exit
  `1` with the typed error; live: the run aborts before the camera
  opens). An uncertain append is reported as
  `persistence unconfirmed` and is **never** automatically retried.
* **What is never persisted or logged:** check-out attempts (unknown
  rules, deferred), unknown faces, crops, embeddings, similarity
  scores, or frames of any kind.
* **Real-camera status:** automated verification of the durable live
  path used injected camera/detector doubles; a supervised owner-run
  `--live --durable` session is the remaining evidence for real-camera
  behavior. Empirical CV accuracy evaluation remains blocked pending
  consented labeled data.

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
