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

## Phase 3 development API (loopback-only)

A minimal FastAPI backend for the Phase 3 work is under construction.
Step 1.3 implements the **API foundation** only:

- `src/api.py` serves the approved eight-endpoint inventory under
  `/api/v1`, of which only `GET /api/v1/health` is implemented (returns
  `{"status": "ok"}`). The other seven approved endpoints
  (`POST/GET /employees`, `GET/PATCH /employees/{employee_id}`,
  `POST /employees/{employee_id}/deactivate`, `GET /attendance`,
  `POST /attendance/{employee_id}/checkout`) exist at their approved
  methods but deliberately answer `501` with the typed
  `NOT_IMPLEMENTED` envelope — they implement nothing, claim no database
  operation, and reach no attendance data until their authorized later
  step.
- Run it locally (binds to the validated loopback address only):

```bash
python tools/run_api.py              # API_HOST=127.0.0.1, API_PORT=8000
python tools/run_api.py --print-bind # print the validated bind, then exit
```

- Settings come from the environment through `src.config`
  (`API_HOST`, `API_PORT`; see `.env.example`). Non-loopback or invalid
  values are rejected by configuration validation itself — the runner
  fails fast (exit code 2, sanitized message) and never starts a
  server.
- **Single-process startup guard (Step 1.4, Windows):** before serving,
  `run_server` acquires an OS-enforced exclusive lock (`msvcrt.locking`,
  one byte at offset 0 of `data/api_guard.lock`, path derived from
  `DATA_DIR`) and holds it for the server's entire lifetime. A second
  API process is refused: the runner exits **3** with a sanitized
  one-line diagnostic and Uvicorn never starts. Orderly shutdown
  releases the lock; abrupt process termination also does (the OS
  releases it with the process), and a stale guard file never blocks a
  later startup. The lock file is idempotent — the initial byte is
  written only when empty, so repeated starts never grow it. Non-Windows
  platforms fail deterministically (no POSIX fallback); this is a
  **local single-process development safeguard, not a security
  boundary**.
- Runner exit codes: `0` success, `2` invalid/non-loopback
  configuration, `3` startup-guard failure (contention or unsupported
  platform), `1` for other unexpected runtime failures.
- Error responses use one stable envelope:
  `{"error": {"code": <machine_code>, "message": <sanitized>}}`, with
  machine codes (`HTTP_ERROR`, `NOT_IMPLEMENTED`, `REQUEST_INVALID`,
  `INTERNAL_ERROR`, and distinct `CONFIG_INVALID` /
  `DATABASE_UNAVAILABLE` / `DATABASE_CONFIG_INVALID` handlers reserved
  for later database-backed steps) deliberately separate from human
  messages. Raw exceptions, tracebacks, DSNs/credentials, and internal
  filesystem paths never appear in responses.
- **Security limitations (by design, in this step):** loopback-only
  binding is a development safety measure, **not** a production
  boundary; there is no authentication, no CORS middleware, and no
  documentation UI (`/docs`, `/redoc`, `/openapi.json` are disabled).
  Any network exposure would require an approved auth/RBAC design in a
  later phase.
- Nothing in this step connects to PostgreSQL, changes the database
  schema, runs a migration, touches the attendance journal, opens a
  camera, or stores biometric data. Starting or importing the API is
  side-effect free (test-enforced).

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
`data/attendance/events.jsonl` (`AttendanceEventStore`). The journal
carries the two existing event types — `check_in` and `check_out`
(`EventType`, Phase 2 P2-6) — each in the same five-key envelope and
the same journal schema version (1). Ordinary verified attendance
continues to use the existing check-in path; the engine never
auto-triggers a checkout merely because an employee has an open
session — a checkout decision only occurs when an explicit checkout
decision reaches the engine's `CHECK_OUT` path. A composition
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
[DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md) Steps 21–26.

### Inspecting the journal (read-only report CLI)

A on-demand, read-only operator utility inspects the journal:
`python tools/attendance_report.py` lists events (chronological by
default, `--journal-order` for append order), filterable by verbatim
employee ID (`--employee`, repeatable) and by UTC date half-open
intervals (`--since`/`--until`/`--day`), with `--summary` for
per-employee × per-day counts, kept separate per event type: the
`CHECK_INS` table counts `check_in` events only, and an additive
`CHECK_OUTS` table counts `check_out` events only when the filtered
selection contains at least one checkout. Pre-P2-6 (check-in-only)
histories keep the prior summary format exactly; no work duration,
session window, or payroll total is inferred — the report is
count-oriented only. It never appends, repairs, or
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

* **What is persisted:** check-in **and check-out** events the real
  attendance engine authorizes (verified identity + the applicable
  independent check-in/check-out cooldown eligibility; a check-out is
  accepted only against a known open session — in memory or, on an
  in-memory miss, via the journal-backed session lookup) — one record,
  the exact `schema_version`/`event_id`/`employee_id`/
  `event_type`/`occurred_at` (UTC) envelope, appended with per-record
  `fsync`. Repeat check-ins are suppressed by recovering previously
  journaled cooldown anchors *before* any event is processed. P2-6
  durable startup restores check-in cooldown state **and** open-
session/check-out-anchor state from **one validated journal
  snapshot**; the journal-backed session lookup reloads persisted
events on every query (never a stale cache). There is no automatic
checkout trigger: a checkout is only ever an explicit engine decision.
* **Failure behavior (fail closed):** corrupt or unreadable journal
  data stops durable mode before any event is processed (smoke: exit
  `1` with the typed error; live: the run aborts before the camera
  opens). Migration/validation errors in the session lookup propagate
  — an employee is never silently treated as having no open session.
  An uncertain append is reported as
  `persistence unconfirmed` and is **never** automatically retried.
* **What is never persisted or logged:** unknown faces, crops,
  embeddings, similarity scores, or frames of any kind.
* **Real-camera status:** automated verification of the durable live
  path used injected camera/detector doubles; a supervised owner-run
  `--live --durable` session is the remaining evidence for real-camera
  behavior. Empirical CV accuracy evaluation remains blocked pending
  consented labeled data.

### Journal compatibility and upgrade/rollback practice

Verified from source (Phase 2, P2-6):

* The journal schema version is **1** (`ATTENDANCE_JOURNAL_SCHEMA_VERSION`)
  and remains **1** with `CHECK_OUT` events — no schema bump or
  migration was needed because the envelope is unchanged and the
  `event_type` field simply carries the new `check_out` value.
* Unknown event types, malformed records, and unsupported schema
  versions are never skipped by the loader: the journal fails with a
  typed error (exact line number reported), in every consumer — the
  report CLI (exit `2`), durable-mode recovery, and the
  journal-backed session lookup.
* **Rollback compatibility risk (verified, treat as real):** releases
  from before P2-6 know only `check_in`. A release at the P2-5
  checkpoint (`38984f6`) rejects the value `check_out` when mapping
  `event_type` through its event model, so `load_events()` fails for
  the **whole journal**, not just the checkout lines: an older release
  running against a journal that already contains checkout events
  reports corrupt data and cannot read any of it (fail closed — the
  session/anchor state for employees with only check-ins is equally
  unreachable). One checkout event therefore makes the journal
  unreadable by the older release.

Recommended operational practice (not automated; nothing here is
executed by the project):

1. **Back up before upgrading:** copy
   `data/attendance/events.jsonl` (and the whole `attendance/`
   directory) before any version change.
2. **Verify the target release's journal compatibility first** — the
   P2-6-era releases read schema-version-1 journals containing both
   `check_in` and `check_out` events; older releases do not (above).
3. **Test recovery on a synthetic copy** before production rollout:
   point a temporary `DATA_DIR` at a copied journal and run the
   durable smoke path.
4. **If a downgrade is required while checkout events exist**, expect
   fail-closed behavior — do **not** manually delete, re-sort, or edit
   journal lines as a workaround.
5. **Preserve the original journal** during any rollback
   investigation and restore a verified backup, or follow a separately
   reviewed migration/recovery procedure; the repository does not ship
   a conversion or downgrade tool.

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
