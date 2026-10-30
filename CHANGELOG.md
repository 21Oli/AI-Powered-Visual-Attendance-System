# CHANGELOG.md

All notable changes to the AI-Powered Visual Attendance System.
Contains **actual implemented changes only** — future work is not listed
as done. Format based on Keep a Changelog.

## [Unreleased] — Phase 3, Step 1.4: Windows single-process startup guard

### Added

* `src/api_guard.py`: Windows-only startup guard — an OS-enforced exclusive `msvcrt.locking` byte-range lock on one byte at offset 0 of `DATA_DIR/api_guard.lock` (path derived from validated `AppSettings.data_dir`). Opened `a+b`; the initial byte is written only when the file is empty, so repeated startups never grow the file. The handle is held for the guarded server's entire lifetime; orderly shutdown releases it, and abnormal process termination is covered by the OS releasing the byte range with the process (a stale guard file never blocks a later startup). Contention/acquisition failures raise the typed `StartupGuardError`; non-Windows platforms raise `StartupGuardUnavailableError` deterministically (no POSIX fallback). Exception messages are fixed sanitized constants.
* `run_server` guard lifecycle: acquisition happens before any request can be served and before Uvicorn is imported (minimal edit in `src/api.py`, disclosed because that file owns `run_server`); the release is the context-manager exit on orderly shutdown. Direct library callers of `run_server` get the same protection; `--print-bind` and app construction never acquire the guard.
* `tools/run_api.py`: guard failures surface as the documented exit code 3 (distinct from configuration exit code 2) with a single sanitized one-line diagnostic — no traceback, no paths.
* `tests/test_api_guard.py`: 14 hardware-free tests using per-test temporary `DATA_DIR` values and real subprocesses — contention across processes, denial while serving, acquisition after orderly release and after abrupt termination, stale-file tolerance, idempotent lock bytes, Uvicorn-never-starts proof, sanitized exit 3, `--print-bind` guard-freedom, and a real graceful-Uvicorn-shutdown release; children are always killed and reaped in `finally`.

### Security notes

* The guard is a **local single-process development safeguard**, not authentication and not a production security boundary; it prevents two API processes serving concurrently on one machine and nothing more.
* Non-Windows platforms fail explicitly rather than silently substituting a weaker Python-level lock; POSIX locking support remains out of scope for Phase 3.
* No database connection, schema change, migration, journal access, camera, or biometric/real-runtime-data access occurs in this step (test-enforced hygiene).

## [Unreleased] — Phase 3, Step 1.3: Loopback-only API foundation

### Added

* `src/api.py`: FastAPI application foundation serving exactly the approved eight `/api/v1` endpoints; only `GET /api/v1/health` is implemented, while the seven non-health approved routes exist at their approved methods and answer a typed `501 NOT_IMPLEMENTED` envelope (no business decision, no database reach, no attendance claim) until their authorized later step. Documentation URLs (`/docs`, `/redoc`, `/openapi.json`) are disabled so the served inventory is exactly the approved eight. No CORS or authentication middleware is installed.
* One stable client-facing error envelope (`{"error": {"code", "message"}}`) with machine-readable codes separate from human messages: `HTTP_ERROR` (registered against Starlette's base `HTTPException`, so framework 404/405 responses use the same sanitized envelope), `REQUEST_INVALID`, `INTERNAL_ERROR` (500 logging trusted diagnostics server-side only), plus `CONFIG_INVALID` / `DATABASE_UNAVAILABLE` / `DATABASE_CONFIG_INVALID` handlers reserved for the database-backed step; raw exceptions, tracebacks, DSNs/credentials, and internal paths never reach responses.
* `tools/run_api.py`: settings-driven single-process development server entry point; binding uses the already-validated `AppSettings.api_host`/`api_port` (never duplicated or bypassed). Non-loopback/invalid `API_HOST`/`API_PORT` fails fast with exit code 2, a sanitized one-line diagnostic, no traceback, and no server start; `--print-bind` prints the validated bind without serving.
* `tests/test_api.py`: offline, hardware-free verification — fresh-subprocess import hygiene (no psycopg/CV/attendance/camera/uvicorn imports and no connection on import or app construction), exact-eight route inventory, ephemeral-loopback real-uvicorn tests over stdlib HTTP with a DB-connection tripwire, sanitized 500 with a credential-bearing synthetic exception, typed-code handler contracts, and runner CLI behavior.

### Security notes

* The API binds loopback-only by validated configuration; loopback binding is a development safety measure, not a production security boundary. There is no authentication and no CORS in this step; any network exposure requires an approved auth/RBAC design in a later phase.
* Nothing in this step connects to PostgreSQL, changes the schema, runs migrations, touches the attendance journal, or accesses camera/biometric data. Importing or starting the API performs no side effects (test-enforced).

## [Unreleased] — Phase 2, P2-6: Check-out event support, journal-backed session recovery, checkout-aware reporting

### Added

* `EventType.CHECK_OUT` (serialized value `check_out`) in the unchanged five-key journal envelope at the unchanged schema version 1; accepted check-outs are persistable through the existing store like check-ins.
* `AttendanceConfig.check_out_cooldown_seconds = 300.0` (validated like the existing cooldowns; `check_in_cooldown_seconds` untouched). The value is an engineering default, not a calibrated organizational policy.
* `src/attendance.py` `AttendanceEngine`: three independently capacity-bounded maps (check-in anchors, check-out anchors, open-session markers), open-session decisions (accepted checkout closes the session and starts the independent checkout cooldown without clearing the check-in anchor; checkout without a known open session is rejected eventless and stateless), and the additive `open_session_lookup` constructor hook for durable adjudication of in-memory misses (strict Boolean contract; typed error on non-Boolean; exceptions propagate; never a silent `False`; never cached). No automatic checkout trigger exists.
* `src/attendance_recovery.py`: pure `fold_latest_events(events)` (per-employee max of `(occurred_at, journal_position)`; equal timestamps by later journal position; UTC datetime comparison; deterministic), shared by `recover_session_state` (open-session/check-out-anchor recovery off the folded latest event), the pre-existing check-in anchor recovery (unchanged contract), and `make_journal_open_session_lookup(store)` (fresh validated `load_events()` per invocation; corruption/I-O errors propagate, never coerced to `False`). Additive engine restoration APIs `restore_open_session` / `restore_check_out_anchor` follow the established P2-3 restoration contract.
* `src/integration_demo.py` durable paths: `recover_attendance_startup_state` performs **one** validated journal snapshot feeding both the P2-3 check-in-anchor replay and the P2-6 open-session/check-out-anchor replay, before any attendance processing, detector load, or camera open; the durable engine's session hook is the journal-backed lookup. Corrupt/unreadable journals stop durable startup before any hardware is touched; default non-durable behavior is unchanged and `--durable` remains opt-in.
* `tools/attendance_report.py --summary`: `CHECK_INS` counts `check_in` events only (pre-P2-6 journals keep the prior output byte-identically) and an additive `CHECK_OUTS` table counts `check_out` events only when the selection contains at least one checkout; count-oriented only (no duration/payroll inference).
* Documentation: README journal-compatibility/upgrade-rollback section, ARCHITECTURE D23, DEVELOPMENT_STATUS Step 26.

### Notes

* **Rollback compatibility risk (verified from source):** releases through the P2-5 checkpoint (commit `38984f6`) map the unknown `check_out` event type to `CorruptAttendanceJournalError` during `load_events()` — a whole-journal fail-closed rejection, not a per-record skip. A journal that already contains checkout events becomes entirely unreadable by such releases; the recommended practice is a verified journal backup before upgrading and a backup restore (never hand-edited journal lines) for rollback. The store's unknown-event-type and torn-tail handling is otherwise unchanged.
* Ordinary verified attendance continues to produce only `check_in` events; a checkout only occurs through an explicit checkout decision request reaching the engine. Test verdicts (2026-10-27 session, hardware-free, synthetic data): recovery 56, core battery 258, integration/durable/report 104, integration-demo CLI 49, report CLI 40 — all passed, exit 0 each; `git diff --check` exit 0. No camera, no real biometric/attendance data, no Git mutations.

## [0.24.0] — 2026-10-24 — Phase 2, P2-5: Opt-in durable composition wired into live and smoke modes (owner-approved wiring milestone)

### Added

* `tools/run_integration_demo.py`: one opt-in `--durable` flag shared by `--smoke` and `--live` (default **off**; default-off invocations keep the exact pre-P2-5 behavior and output). Smoke mode propagates the flag into `run_smoke` and, when enabled, prints a clearly-labeled durable summary (cooldown anchors recovered, the four persistence-outcome counts, and a note that the run's journal lives in the temporary directory removed on exit). A durable smoke run stopped by a journal-load or recovery error exits `1` with a clear `durable smoke mode stopped before any event processing` message — fail closed, never silently degraded. Live mode passes the flag through `LiveModeConfig.durable`; a journal-load or recovery failure is reported through the existing live-failure path (nonzero exit) because it is raised before the camera ever opens.
* `src/integration_demo.py` — `run_smoke`: new keyword-only `durable=False` and `wall_clock=None`. When durable, the real Step 13 engine is composed with the P2-1 `AttendanceEventStore` via the unchanged P2-2 `DurableAttendanceService`, and the P2-3 `recover_cooldown_anchors` replay runs **before any frame is processed** (a journal-load or recovery error propagates — durable mode stops before processing events, never silently downgraded). The documented four outcome states are reported verbatim (nothing redefined); created events are appended exactly when the engine creates them (per-record fsync, uncertainty never retried); the synthetic but real-shaped fixture behavior is unchanged. `SmokeRunResult` gains additive default fields `durable_outcomes` (per-frame persistence statuses in frame order; empty when flag-off) and `durable_recovered_anchors`; the no-event honesty path (no fabricated events) is unchanged and now reachable in durable runs too. The smoke journal is scoped to the run's temporary `AppSettings` — the smoke temporary directory is removed with it on exit.
* `src/integration_demo.py` — `run_live`: `LiveModeConfig` gains an additive `durable=False` attribute (default path untouched: no store constructed, no journal directory read or written, recovery never called). With the flag on, the durable engine/store/recovery/service composition runs **before** any hardware acquisition — recovery completion precedes the detector load and camera open, and journal-load or recovery errors stop the run before anything is opened (fail closed). Verified frames carry the same `VERIFIED …` line as before and additionally print one `durable:` line for `PERSISTED` / `PERSISTENCE_REJECTED` / `PERSISTENCE_UNCONFIRMED` outcomes (`NO_EVENT` stays silent, matching the unknown-face reporting convention); uncertain appends are never retried; unknown-face handling, recognition thresholds, temporal policy, and event contracts are exactly as before. `run_live` gained a keyword-only `wall_clock` injectable (default `datetime.now(UTC)`) solely for the durable branch's engine timestamps and recovery age computation.
* `tests/test_durable_live_mode.py` (16 tests) plus 13 new tests in `tests/test_run_integration_demo_cli.py`: flag parsing/default-off/position-independence and exact propagation into `run_smoke` (both flag states) and `LiveModeConfig` (with the other live options provably untouched); durable smoke happy path (exact one-event persistence, `("no_event","no_event","persisted","no_event","no_event")` outcome sequence, wall-clock timestamp agreement, durable detail segments, five-key journal privacy envelope); restart simulation (journal carried to a fresh run → recovery restores the anchor → frame-3 suppression, nothing re-appended); cooldown boundary (journaled age 299 s suppressed; exact 300 s expired → fresh event persisted); persistence rejected/uncommitted surfacing with exactly one append attempt (never retried) and a provably unchanged/empty journal; corrupt journal and unsupported schema version stopping durable smoke typed-ly before processing; flag-off smoke never creating the journal directory and never calling recovery (probe-tested); durable live fail-closed ordering proven with recording camera/detector doubles (corrupt journal → error before any hardware load), durable live clean run printing the recovery summary and writing nothing, and flag-off live leaving the journal untouched; durable smoke CLI failure exit 1 with the typed error surfaced.

### Notes

* **Default-off byte discipline (tested):** without `--durable` no journal directory/file is created anywhere, `recover_cooldown_anchors` is never invoked, and smoke/CLI output is exactly the pre-P2-5 output; the durable summary and `durable:` details appear only in flag-on runs. Durable smoke journals inside its temporary directory (removed with it); durable live journals to the configured `data/attendance/events.jsonl` (UTC timestamps only, unchanged schema). Failure handling: recovery/journal-load errors fail closed in both modes (smoke CLI exit 1 with typed message; live raises through the existing failure path before the camera opens); uncertain appends are reported and never automatically retried (the P2-2 contract).
* Phase 2 wiring honesty: P2-1 (store), P2-2 (composition), P2-3 (recovery), and P2-4 (report CLI) existed unchanged and were wired by P2-5 into opt-in live/smoke paths only — the report CLI is not part of any run loop (it remains an on-demand operator tool). No thresholds changed; no unknown-face biometrics are stored (the unknown-face path is untouched); no frames, crops, embeddings, or similarity scores are logged; no check-out/shift/lateness/daily-dedup business rules were added (separate milestone, sequence unchanged). **Real-camera durable behavior is not verified**: the owner may separately run `--live --durable` and record the outcome — no such evidence exists yet. Empirical CV accuracy evaluation remains BLOCKED pending the required consented labeled dataset and evaluation procedure. Focused suites 65 passed (13 new CLI + 16 new durable-mode + previous CLI tests) and related suites 260 passed; full suite 994 passed (965 prior + 29 new), exit 0 each (`pipefail`-captured); camera not opened during any automated test; no real biometric/enrollment/attendance data accessed; nothing staged or committed (Git state untouched).

## [0.23.0] — 2026-10-24 — Phase 2, P2-4: Read-only attendance report CLI (operator inspection only, no live wiring)

### Added

* `tools/attendance_report.py`: a read-only operator CLI over the unchanged P2-1 journal (`<DATA_DIR>/attendance/events.jsonl`). Lists events in deterministic chronological order by default (`occurred_at`, ties resolved by original journal position — the project's documented tie-break) with `--journal-order` as the explicit append-order alternative; verbatim repeatable `--employee` ID filtering (IDs are never normalized); UTC date filtering as half-open intervals (`--since` inclusive at that date's midnight; `--until` inclusive over its whole day via a next-midnight exclusive bound — fractional seconds fully covered; `--day` = both together, mutually exclusive with the pair); `--summary` per-employee × per-day counts (day order, then verbatim employee ID). Duplicate `event_id` records are displayed as-is, never de-duplicated or warned about (the store's documented limitation is reported, not hidden). Output carries exactly the four schema fields (UTC timestamp, event type, employee ID verbatim, event ID verbatim) — no scores, names, or biometric content.
* Deliberate exit contract, argparse's default error exit overridden: `0` success (including an empty journal and an incomplete trailing record — the torn tail is surfaced as a stderr warning while valid preceding rows print, per the loader's exclude-the-fragment semantics, never repaired); `2` corrupt journal / unsupported schema version / unreadable data — the store's typed error goes to stderr and NO event rows are printed (a failed load is never presented as an empty report); `3` invalid arguments or dates — checked before any journal access; `--help` follows the untouched default argparse path (prints help, 0).
* Import/execution hygiene (asserted by tests): importing the tool module loads no `src` modules at all (the store imports are deferred into `main()`, because the store's module graph transitively includes recognition-layer types); executing a full report imports the P2-1 store but never constructs a camera, detector, or recognizer model (proven by turning `cv2.VideoCapture` / `cv2.FaceDetectorYN` / `cv2.FaceRecognizerSF` / `onnxruntime.InferenceSession` into hard failures in a test); importing creates no data-directory tree.
* `tests/test_attendance_report_cli.py` (36 tests): empty/missing/zero-byte journals exit 0; single and multi-event listings; chronological default with a journal-position tie-break; `--journal-order` distinct ordering; verbatim/repeatable employee filtering with no-match handling; half-open date boundaries including fractional seconds at 23:59:59.999999Z and the next-midnight exclusive edge; deterministic `--summary` (row order and counts asserted, filter-respecting); torn-tail warning with rows; corrupt mid-file and unsupported-schema → exit 2 with no rows and byte-identical journal; invalid dates/`--day` conflicts/empty interval → exit 3 with untouched journal bytes; `--help` → 0; journal bytes byte-identical after every success-path invocation; subprocess import hygiene (no `cv2`/camera/biometric modules in `sys.modules`, no data-dir creation); in-process no-initialization guard.

### Notes

* Explicit limitations: UTC-only date display and filtering (site-local timezone support deliberately deferred — the journal stores UTC only); duplicate `event_id` lines print as-is by design. The CLI is **not wired into the live attendance workflow** (no caller; the live demo remains unconnected to persistence, unchanged); it never appends, truncates, repairs, rewrites, or locks the journal. Journal schema, store behavior, recovery API, and the P2-2 four-state outcome contract are unchanged. No new dependencies (stdlib `argparse`/`datetime` only); no files under `src/`, `requirements.txt`, or `pytest.ini` touched. Focused CLI suite 36 passed and full suite 965 passed (929 prior + 36 new), exit 0 each under `pipefail`; camera not opened; no real biometric/enrollment/attendance data accessed; nothing staged or committed.

## [0.22.0] — 2026-10-23 — Phase 2, P2-3: Restart recovery of attendance cooldown anchors (read-only replay, no live wiring)

### Added

* `src/attendance_recovery.py`: `recover_cooldown_anchors(engine, store, *, wall_now)` — deterministic, read-only restart recovery. Loads the journal in full through the unchanged P2-1 `load_events()` **before** any engine mutation (a failed load can never leave a partially recovered engine; `CorruptAttendanceJournalError` / `UnsupportedJournalSchemaVersionError` propagate unchanged — a failed load is never treated as an empty journal), selects per employee the latest persisted `CHECK_IN` by UTC `occurred_at` (ties by journal position; deterministic under out-of-order records and the store's documented duplicate-`event_id` retry limitation), and restores each unexpired anchor via the engine's new additive API. Nothing is appended, rewritten, or created; no attendance event is emitted; the torn-tail `incomplete_trailing_record` flag is passed through on the frozen `CooldownRecoveryReport` (restored IDs, expired/clamped counts, records scanned). Log lines carry counts only — never employee IDs, never biometric content.
* `src/attendance.py` (additive only): `AttendanceEngine.restore_suppression_anchor(employee_id, *, elapsed_seconds)` — places one anchor at `monotonic_now - elapsed_seconds` in the current run's monotonic domain; returns `False` when the age is already at or beyond the configured cooldown (expired — nothing stored). The engine remains the single cooldown authority (its own `check_in_cooldown_seconds` decides expiry; LRU bounded-state policy identical to `update()`); a wall timestamp is never stored as or compared as a monotonic value. Existing engine behavior is untouched (the monotonic read/validation was factored into `_monotonic_now()` verbatim).
* `tests/test_attendance_recovery.py` (28 tests) and a new `TestRestoreSuppressionAnchor` class in `tests/test_attendance_engine.py` (16 tests): empty/missing journal recovery with zero side effects, one-event restore with exact remaining-cooldown boundary, per-employee independence, latest-event selection, real-chain replay (engine + store + `DurableAttendanceService` over a temp journal — suppressed frames and non-verified results never become replayable events), expiry at/after the exact boundary, out-of-order and duplicate-ID determinism, torn-tail exclusion with flag pass-through, corrupt/unsupported-schema all-or-nothing failure (no false success, no partial state), journal bytes byte-identical after replay, the explicit clock-skew policy (backward → clamp to full cooldown; forward → expired worst case one extra event), wall↔monotonic mapping, and replay-then-process matching pre-restart behavior.

### Notes

* Clock-skew policy (explicit, conservative, documented in D20 and the module): wall clock moved backward across a restart → negative age clamped to zero (suppress for the full cooldown — worst case one *delayed* legitimate check-in, never a duplicate); wall clock moved forward → expired anchors are simply not restored (worst case one additional check-in, the engine's documented bounded-state worst case). Honest limit: cross-run cooldown precision is bounded by wall-clock reliability between runs; within a run, suppression remains monotonic-exact. Recovery is not wired into live mode (no caller); the P2-2 four-state outcome contract and the README live-mode statement are unchanged. No engine decision semantics, journal schema, threshold, or dependency changed. Focused suite 96 passed (28 recovery + 16 new engine tests plus the two modules' existing tests; engine/store/persistence re-runs green), relevant suites 212 passed, full suite 929 passed (exit 0 each).

## [0.21.0] — 2026-10-23 — Phase 2, P2-2: Durable attendance composition (engine + store, no live wiring)

### Added

* `src/attendance_persistence.py`: `DurableAttendanceService` — composes the unchanged Step 13 `AttendanceEngine` (decision authority) with the unchanged P2-1 `AttendanceEventStore` (persistence authority). `process(result)` feeds the temporal result to the engine unmodified; a decision without an event appends nothing; a created event is appended exactly once. A frozen `DurableAttendanceOutcome` reports exactly four explicit states: `NO_EVENT` (verbatim decision carried), `PERSISTED` (append/fsync confirmed), `PERSISTENCE_REJECTED` (the store raised `AttendanceStoreInputError` before any write — a confirmed failure: journal provably unchanged, record definitively absent), and `PERSISTENCE_UNCONFIRMED` (any other store error — an uncertain outcome; the original exception object is preserved unmodified with the exact event and a short reason — never presented as a durable check-in, and **never automatically retried**, since the journal may already contain the record and a retry could duplicate it). Other store exceptions and engine `AttendanceInputError`s propagate unchanged. Collaborators are structurally validated (callable `update`/`append`), so injected doubles work; construction performs no filesystem work; log lines carry statuses only, never employee IDs.
* `tests/test_attendance_persistence.py`: 19 deterministic, hardware-free tests — the three-outcome matrix (no-event ×3 statuses with zero appends and verbatim decision carriage; persisted exactly-once same-object append; unconfirmed with identity-preserved exception), no automatic retry across repeated failures, store input-error family mapping, non-store programming errors propagating unlabeled, engine exception propagation with zero appends, frozen outcome, construction validation and no-side-effects, and real-engine integration with injected clocks (VERIFIED → one exact event; same-instant suppression → no append; exact 300 s cooldown boundary → second event).

### Notes

* No attendance decision, store contract, schema, threshold, or dependency changed; the live demo, smoke mode, and CLI tools are untouched — the service has no caller yet and wiring it into live mode is explicitly future work. Engine cooldown anchors remain engine-instance-lifetime; restart recovery is future work. Focused suite 19 passed, relevant suites 166 passed, full suite 883 passed (exit 0 each).

## [0.20.0] — 2026-10-22 — Phase 2, P2-1: Durable attendance event store (storage only)

### Added

* `src/attendance_store.py`: `AttendanceEventStore` — an append-only JSON Lines journal persisting existing, already-validated `AttendanceEvent` values at `<DATA_DIR>/attendance/events.jsonl` (path derived from `AppSettings` like every other storage root). On-disk record schema is exactly five keys (`schema_version: 1`, `event_id`, `employee_id`, `event_type`, `occurred_at` in UTC ISO-8601); the same five-key envelope is enforced on load, so records carrying extra keys — biometric, diagnostic, or future — are typed errors, never silently reinterpreted. No face images, crops, embeddings, similarity scores, or unknown-person data pass through this module.
* Torn-tail recovery contract: after an interrupted append, loading excludes an incomplete trailing fragment and reports it (`incomplete_trailing_record=True`) instead of hiding it; corruption anywhere else raises `CorruptAttendanceJournalError` with the exact journal line number and is never silently skipped. Appending over a torn tail repairs it first — a trailing fragment that is a fully valid record is salvaged by appending the missing newline (the record is preserved); otherwise the complete-line prefix is republished via the staged-temp-file → fsync → atomic `os.replace` pattern (the Step 09 precedent), the store's only rewrite path. A failed repair raises and leaves the journal byte-unchanged.
* Append durability policy: normal appends never rewrite the journal — `open("a")` + `flush()` + `os.fsync()` per record; a record counts as persisted only after fsync returns, and every directory/write/flush/fsync failure raises `AttendanceStorePersistenceError` with the caller told to treat the event as unconfirmed (documented, and pinned by a test, that a record may still physically be present after an fsync failure — appends cannot be retracted).
* `src/config.py`: additive derived path `AppSettings.attendance_dir` (`data_dir / "attendance"`) mirroring `embeddings_dir`; no existing configuration, validation, or behavior changed.
* `tests/test_attendance_store.py`: 47 deterministic, hardware-free tests (append/loading/order/per-type round-trips, verbatim IDs and timestamps, load-is-read-only, no-construction side effects, privacy envelope, full load-validation matrix with line numbers, unsupported schema versions, torn-tail salvage/truncate/failed-repair semantics, typed error hierarchy).

### Notes

* Storage only in this step: the store is not wired into the live demo or the attendance engine yet (next phase step), and no existing attendance behavior changed — the full suite passes unchanged (858 tests = 811 prior + 47 new). Honest limits recorded: no cross-process file locking or `event_id` dedup (retry/once-policy belongs to the composition layer), fsync covers the file — not the directory entry — and there is no query/audit API yet; empirical CV evaluation remains separately blocked and untouched.
* Post-audit fix (same day): an ID carrying a lone Unicode surrogate passes event construction but cannot be UTF-8 encoded — `append()` now raises the documented typed `AttendanceStoreInputError` (cause preserved, journal verified byte-identical, no partial data) instead of an unwrapped `UnicodeEncodeError`; four new boundary tests pin surrogate rejection, zero-byte-journal loading, directory-at-journal-path behavior in both directions, and load of a complete valid trailing fragment missing only its newline (full suite now 864 tests).

## [0.19.0] — 2026-10-22 — Phase 1, Step 20: Unknown-face handling wired into live mode (report-only, real handler)

### Added

* `src/integration_demo.py` (`run_live`): the live pipeline now integrates the existing `UnknownFaceHandler` (Step 12) through its authoritative `observe_temporal` contract — the same pattern smoke mode already uses. The real handler is constructed once per live session (default frozen `UnknownFaceConfig` with the bounded 30 s non-refreshing cooldown and `max_sessions` LRU cap; in-memory only, no persistence). On every full-chain frame (single face, quality-accepted, recognizer available) the frame's temporal verification result is fed to the handler, and live console output gains deterministic report-only lines: `unknown-face outcome observed (…)` for the first unresolved observation of an episode, `suppressed (…; suppressed_frames=N)` for repeats inside the cooldown, and `episode_ended (suppressed_frames=N)` when a temporally verified identity resolves the episode. `NO_ACTION` (nothing unknown, or a verified identity with no active episode) stays silent. Frames without detection/quality rejection never fabricate temporal evidence (the live loop never ran the recognizer on them before and still does not); single MATCHED frames remain non-verified per the Step 11 streak contract; recognition, temporal verification, attendance decisions, duplicate suppression, and camera cleanup are unchanged.
* `run_live` gained a keyword-only `monotonic` injectable clock (same default-resolution convention as `run_smoke`; default `time.monotonic`) solely so the episode-cooldown boundaries stay deterministic in hardware-free tests.
* `tests/test_integration_demo.py`: 9 focused hardware-free tests — pure `unknown-face outcome` formatting (observed/suppressed/episode-ended, count appendix, no-appendix cases); the report helper against the real `UnknownFaceHandler` with a deterministic clock (first unresolved observation reported once, repeats suppressed with the running count, a verified identity resolving the episode with EPISODE_ENDED and without leaking the employee ID into the unknown-face line, and NO_ACTION silence); and a full live-loop wiring test with injected camera/detector/assessor/embedder/recognizer/verifier/attendance doubles asserting the exact OBSERVED → SUPPRESSED → SUPPRESSED → EPISODE_ENDED → fresh OBSERVED console sequence, exactly one attendance decision (verified frame only), and clean camera release.

### Notes

* Report-only: the unknown-face lines print statuses and suppressed-frame counts only — never crops, embeddings, identities, or other quality metrics, and nothing is written to files or logs by this wiring. The live demo's attendance semantics are untouched: an unresolved observation can never authorize an event.

## [0.18.0] — 2026-10-20 — Phase 1, Step 18: Operator deletion CLI (preview → two-stage confirmation → derived-path scoped removal → verified absence)

### Added

* `tools/delete_enrollment.py`: operator CLI removing exactly one employee's enrollment artifacts (stored embedding `.npz` + `.meta.json`, the `employees_dir` manifest directory published by Step 07, and the `face_samples` directory). No deletion API exists in the existing services, so removal is strictly *derived-path* file/directory deletion built on the public validation APIs: the ID is validated through `src.enrollment.safe_dir_name` (exact Employee rules; traversal/`..`/dot-forms rejected before any filesystem work), every removed path is derived from the configured `AppSettings` roots only, and defensive containment checks re-verify each derived parent against the resolved root. Preview (plain run) prints present/missing status per artifact group with zero changes (exit 0); `--execute` without `--confirm-delete` is refused (exit 2, nothing runs); both separate flags together trigger deletion and never bypass ID validation or the target-existence check (a nonexistent enrollment is refused, exit 1). Removal order mirrors enrollment's publish order in reverse (embedding record → manifest → samples), so a mid-deletion crash can only leave the publish-safe orphaned-samples state, never a declared enrollment without images. Failures report exactly what was removed and what remains (exit 1, never fabricated success); success requires the ID to be provably absent through the real `EmbeddingStorageService.list_employee_ids()` plus direct artifact-existence checks. File contents (crops/vectors) are never read, printed, or logged. Exit codes: 0 success, 1 typed failure, 2 confirmation/argparse refusal.
* `tests/test_delete_enrollment.py`: 10 focused categories (all tmp-dir + injected double): invalid/malicious ID rejection before any filesystem work; nonexistent enrollment refusal with zero changes; preview makes no changes; missing individual artifacts tolerated; `--execute` without `--confirm-delete` refused; successful deletion of all artifact groups; deletion/storage failure with partial-failure reporting; final candidate-verification failure (ID still listed → no success); isolation (another employee's files and shared model files untouched); success only when the target is no longer discoverable.

### Notes

* Deletion is irreversible: recovery is possible only by re-enrolling a re-consenting participant (documented in README). The live demo remains never-enrolling and never-deleting; both operations stay operator-driven. No thresholds, model files, dimensions, or service contracts were changed.

## [0.17.0] — 2026-10-20 — Phase 1, Step 17: Operator enrollment CLI (consent-gated enroll → bridge → candidate verification)

### Added

* `tools/enroll_employee.py`: operator CLI connecting the existing Step 07 `EnrollmentService.enroll` to the existing Step 16 `embed_published_samples` bridge — one command makes a consenting employee a recognition candidate. Composition only: no new recognition math, thresholds, persistence, camera, or model behavior. Flow: mandatory `--confirm-consent` gate (without it the tool does literally nothing — no prerequisite probe, no camera, no validation — and exits 2) → prerequisite probing for the operator-supplied `models/yunet.onnx` / `models/sface.onnx` (clear abort before any capture) → the existing bounded enrollment session (its own duplicate refusal and typed errors surface unchanged) → the bridge for exactly one embedding under `replace=False` → final proof that the ID is discoverable via `storage.list_employee_ids()` (the live recognizer's candidate path) before printing success. Failure reporting is honest and never fabricates success: duplicate enrollment/existing embedding → refused, nothing changed; embedding failure after a completed capture → the published enrollment stays on disk (legitimate Step 07 data) and the recovery path is printed. No crops, vectors, or embeddings are ever printed or logged. Exit codes: 0 success, 1 typed failure, 2 argparse/consent refusal.
* `tests/test_enroll_employee_cli.py`: 8 focused CLI categories (all mocked, hardware-free, biometric-free): consent declined does nothing; consent accepted runs enroll → bridge in order; real-validation ID/name rejection with the camera factory never touched; duplicate enrollment and duplicate existing embedding refusal (nothing overwritten); capture/enrollment failure (no embedding work); embedding generation/storage failure; final candidate-verification failure (no success message); success only when the ID appears in `list_employee_ids()`. Extended 2026-10-21 with the frame-size categories (parser surface, camera-config composition, flow/main wiring), still hardware-free.
* Opt-in live-demo sharpness diagnostic (2026-10-21): `tools/run_integration_demo.py --live --report-quality-metrics` — an explicit, disabled-by-default console-only flag under which frames the per-face gate rejects **specifically for blur** additionally print the already-measured sharpness value and the configured threshold, e.g. `[frame   12] rejected by quality: blur (sharpness=18.3, threshold=50.0)`. The metric is never recomputed, never saved to files, and never appended to `data/logs`; with the flag absent the output is byte-identical to the pre-diagnostic format. Motivated by the measured blur-rejection pattern on the development camera whose actual numeric values were computed but never surfaced; the values enable evidence-based Phase-2 threshold-calibration discussion and never auto-tune anything. Implementation: config flag on `LiveModeConfig`, a pure `_format_quality_rejection` formatter (plain line when the sharpness metric is unmeasured — never an invented value), CLI wiring that injects the keyword only when the operator passes the flag.
* Failure-output diagnostic tally on `tools/enroll_employee.py` (2026-10-21): the enrollment-failure line now appends a deterministic `rejections by reason` summary — counts of the already-recorded `EnrollmentResult.rejections` by their stable `FaceQualityReason` value, reasons lexicographically sorted (e.g. `rejections by reason: blur=1, no_faces=2, too_small=1`); omitted entirely when the session recorded no rejections. Diagnostic-only: prints reason names and counts, never frame indices, crops, quality metrics, or any biometric data; capture logic, acceptance thresholds, camera configuration, persistence, and success behavior are all unchanged. Motivated by the measured 60-attempt/0-sample failure whose specific rejection split was recorded in the result but hidden from the operator output.
* `--frame-size WIDTHxHEIGHT` on `tools/enroll_employee.py` (2026-10-21): optional camera frame-size override for the enrollment capture session — the same validated parser as `tools/run_integration_demo.py --frame-size`, reused rather than duplicated. Motivated by a measured failure on the development machine: the index-0 camera reports 640×480 while the configured default is 1280×720, so under the default the capture session aborted in `CameraService.open()` before any frame ("frames attempted: 0"; nothing published). The override changes only the *requested* frame size; the device index, all camera validation (`CameraOpenError` when the device cannot apply the requested size), and the configured default resolution remain unchanged.

### Notes

* The live demo (`tools/run_integration_demo.py --live`) remains never-enrolling; enrollment stays operator-driven and consent-gated. Real-camera recognition accuracy is still not claimed: automated tests exercise wiring with injected doubles only, and a live consented participant run remains the sole proof of real recognition.

## [0.16.0] — 2026-10-19 — Phase 1: Enrollment-to-recognition bridge (sample PNGs → one stored embedding per employee)

### Added

* `src/enrollment_bridge.py`: `embed_published_samples(employee_id, *, embedder, storage, settings)` — connects Step 07 enrollment to Step 10 recognition without duplicating any service: requires the published Step 07 `employee.json` eligibility marker (schema version 1, exact ID match, bool `is_active`), decodes every published face-sample PNG fail-closed (one unreadable sample aborts before any write), embeds each via the existing `FaceEmbedder` (typed errors surface unchanged), aggregates as the cosine centroid (per-sample L2 normalization then unit mean — verified compatible with the raw-feature cosine decision metric and the storage norm contract), and persists exactly one embedding via `EmbeddingStorageService.save(..., replace=False)` (duplicate records abort before any embedding work; no force-replacement path). Success re-proves recognition-side consistency through `storage.load` and `storage.list_employee_ids()`. No vectors/crops are ever logged or returned; the result carries facts only (ID, dimension, sample count).
* `tests/test_enrollment_bridge.py`: 16 focused tests (all synthetic non-biometric inputs, faked embedders, tmp dirs): successful bridge + candidate discovery through the real `FaceRecognitionService` (MATCHED) and `list_employee_ids`; exact cosine-centroid aggregation; missing/contradictory/incompatible manifests; zero samples and unreadable samples (fail-closed); ID-form rejection; non-storage instance rejection; embedder/dimension/zero-norm/canceling-sample failures persisting nothing; duplicate protection both via pre-existing records and second bridge runs; storage persistence failure.

### Notes

* No thresholds, model files, embedding dimensions, storage contracts, or existing services were changed. End-to-end *live* recognition after an enrollment still requires an authorized operator enrollment with a consenting human participant (real camera, real models); unit tests use synthetic vectors only and do not claim real-model recognition accuracy.

## [0.15.0] — 2026-10-14 — Phase 1, Step 15: Integration demo (integration & smoke green; live-camera real-world results not obtained; empirical CV evaluation still blocked)

### Added

* `src/integration_demo.py`: the Phase 1 integration orchestrator —
  composes the existing approved components (Steps 04–13) end to end
  (capture → detect → quality → embed → recognize → temporal verify →
  unknown-face handling → attendance event) with **no** new pipeline
  math or thresholds. Two explicitly separated modes: `run_smoke`
  (deterministic, hardware-free, temp-dir-only fixtures; real
  downstream engines; honest no-event outcomes) and `run_live`
  (explicitly opt-in real-camera run with prerequisite probing, a
  clearly-labeled partial demo while `models/sface.onnx` is absent,
  and guaranteed resource release on every exit path).
* `tools/run_integration_demo.py`: CLI entry point (`--smoke` default;
  `--live` opt-in with `--camera-index`, `--no-window`,
  `--max-frames`; optional `--frame-size WIDTHxHEIGHT` camera-frame
  override for `--live`, validated as two positive integer dimensions
  with a clear CLI error otherwise). Verified via `--help` and a
  `--smoke` run.
* `tests/test_integration_demo.py`: 20 focused tests covering the
  single-event happy path (exact ID `"007"`), per-stage checkpoint
  coverage, boundary-injection integrity, no-event guarantees for
  quality-rejected/no-face/unknown-evidence runs, real-data safety
  (repository `data/` untouched before/after), typed input errors,
  and the live mode's abort/prerequisite/headless paths.

### Notes

* Only a real, temporally `VERIFIED` identity reaches the real
  attendance engine; the integration layer never fabricates events
  or recognition results. Real camera/SFace inference was not
  exercised at Step 15 recording time (`sface.onnx` was then absent);
  empirical CV evaluation remains blocked exactly as recorded for
  Step 14.

## [0.14.0] — 2026-10-13 — Phase 1, Step 14: Testing & evaluation (software regression green; empirical CV evaluation blocked)

### Added

* `notebooks/09_phase1_evaluation.ipynb`: reproducible, privacy-safe
  Phase 1 evaluation notebook — environment & artifact availability
  (yuNet `yunet.onnx` PRESENT, `sface.onnx` MISSING; nothing is ever
  downloaded), safe regression-suite instructions with an opt-in
  runner cell, software-level detection contract checks + documented
  IoU helper + explicitly opt-in local YuNet smoke check on a
  synthetic frame, synthetic-quality threshold rows and face-count
  policy, synthetic-identity recognition decision rows,
  temporal/unknown-face/attendance behavior rows with injected fake
  clocks (no sleeps), machine-dependent software-overhead timing and
  an explicit limitations/spoof-boundary subsection. No camera is
  opened, nothing enrolled, the production `data/` store untouched,
  biometric files never read.
* `reports/phase1_evaluation.md`: evidence-based evaluation report;
  every value labeled MEASURED / TEST COVERAGE / NOT RUN / BLOCKED /
  NOT MEASURED with reasons — regression results, artifact table,
  detection (precision/recall NOT RUN — no annotated dataset),
  synthetic quality rows, recognition decisions with FAR/FRR NOT
  MEASURED (missing embedding model), behavior rows for temporal/
  unknown-face/attendance incl. exact cooldown boundaries, the
  documented-but-unexecuted spoof scenario plan (no resistance
  claim), risks, and reproduction instructions.

### Notes

* Step 14 status is **not** claimed fully passed: software regression
  (654 tests) is green; empirical CV evaluation (accuracy, FAR/FRR,
  real-lighting quality, spoof resistance, camera latency) remains
  blocked on the missing `sface.onnx` artifact and consented labeled
  data. No model, dataset, or biometric files were added.

## [0.13.0] — 2026-10-13 — Phase 1, Step 13: Attendance event engine

### Added

* `src/attendance.py`: `AttendanceEngine` — deterministic,
  independently testable attendance-event engine strictly on the
  approved Step 11 trust boundary: only a structurally valid
  `VERIFIED` temporal result can authorize a check-in; raw
  recognition results (including `MATCHED`) are rejected input;
  `COLLECTING`/`INTERRUPTED` yield `NOT_VERIFIED`; malformed inputs
  and invalid clock outputs fail safely with typed
  `AttendanceInputError` (no event, no state change, no fabricated
  success). Optional injectable `active_lookup` (the Step 10 hook
  pattern; no new manifest/storage reads) yields
  `INACTIVE_EMPLOYEE` for inactive verified employees and is never
  invented when absent.
* Structured event & decision: frozen `AttendanceEvent(event_id,
  employee_id, event_type, occurred_at)` — `EventType.CHECK_IN` only
  (no check-out/shift/lateness/payroll logic in this step);
  timezone-aware `occurred_at` normalized to UTC (naive rejected);
  documented `uuid.uuid4().hex` event-ID default with validated
  injectable factory; no biometric data carried. Frozen
  `AttendanceDecision(status, event, reason)` with
  `{EVENT_CREATED, NOT_VERIFIED, INACTIVE_EMPLOYEE, SUPPRESSED}`
  — `event` is non-`None` only on `EVENT_CREATED`.
* Duplicate suppression (in-memory, bounded, non-refreshing): one
  `CHECK_IN` per employee per `check_in_cooldown_seconds=300.0`
  (initial engineering value, not a calibrated policy); exact
  boundary (`elapsed < cooldown` suppressed, `== cooldown` new
  event); anchors on an injectable monotonic clock — never refreshed
  by repeated frames; per-employee independence; LRU-bounded at
  `max_tracked_employees=64`. **Documented restart limitation:**
  anchors live for the engine instance only — this is not durable,
  cross-process exactly-once protection.
* Explicit clock separation: `clock_wall` (timezone-aware UTC
  datetimes, naive rejected) for event timestamps vs
  `monotonic_clock` (finite values, invalid outputs rejected) for
  suppression — backward wall-clock movement cannot bypass duplicate
  protection or expire anchors. No sleeps in tests (injected fake
  clocks).
* `tests/test_attendance_engine.py`: 52 deterministic,
  hardware-free tests covering the 30-item Step 13 checklist (trust
  boundary, event schema, suppression boundaries and expiry, config
  validation, clock failures and backward time, reset semantics,
  instance isolation, factory-failure atomicity, bounded LRU state,
  and side-effect-free import/construction).

### Notes

* The engine is not connected to the camera loop; no orchestration,
  dashboard, database/CSV persistence, or audit trail was added.
  Identity/recognition/attendance responsibilities remain separate.
  Step 14 (testing & evaluation) and Step 15 (integration demo) were
  not started.

## [0.12.0] — 2026-10-13 — Phase 1, Step 12: Unknown-face handling

### Added

* `src/unknown_face_handler.py`: `UnknownFaceHandler` — conservative,
  deterministic, in-memory unknown-face handling strictly on top of
  the approved Step 10 recognition and Step 11 temporal-verification
  contracts. Two documented entry points: `observe_temporal()` is the
  authoritative trust boundary (only a `VERIFIED` temporal result
  resolves an unresolved episode as a known, temporally verified
  presence; outcomes never carry an identity), `observe_recognition()`
  is observability-only (a raw `MATCHED` is classified
  `MATCH_UNVERIFIED` — never an identity). Frozen
  `UnknownFaceOutcome(status, observation, suppressed_frames, reason)`
  with `UnknownFaceStatus.{NO_ACTION, OBSERVED, SUPPRESSED,
  EPISODE_ENDED}` and one-to-one `UnresolvedObservation` values; no
  employee IDs, scores, frames, crops, or names ever carried.
* Deterministic episode/cooldown policy: first unresolved observation
  opens one `OBSERVED` alert per caller-managed session; repeats
  within `episode_cooldown_seconds` are `SUPPRESSED` (counted); the
  cooldown is non-refreshing and the episode expires exactly at
  alert + cooldown (injectable monotonic clock — no sleeps in
  tests); a VERIFIED identity ends an active episode
  (`EPISODE_ENDED`); `reset(session_key=None)` ends one or all
  episodes explicitly.
* Bounded state: at most `max_sessions` episode records (LRU
  eviction), no unbounded histories; the project has no stable
  face-track identifier, so suppression is a documented
  unresolved-session policy, not evidence that repeated frames show
  one unknown person (no tracker added; no cross-person merging).
* `src/config.py`: frozen `UnknownFaceConfig` + `UNKNOWN_FACE` —
  `episode_cooldown_seconds=30.0`, `max_sessions=8`,
  `max_session_key_length=128`, all **initial engineering defaults**
  pending Phase 2 calibration; strict validation (bools rejected for
  numeric fields, NaN/inf/non-positive cooldowns rejected).
* `tests/test_unknown_face_handler.py`: 56 deterministic, hardware-
  free tests (synthetic results, injected fake clock) covering the
  verified/unverified boundary for every status, identity-free
  classification, cooldown boundaries and non-refreshing expiry,
  reset/EPISODE_ENDED semantics, bounded state and LRU eviction,
  session isolation and key validation, fail-safe malformed inputs,
  frozen types, and side-effect-free import/construction.

### Notes

* Unknown-face handling does **not** record attendance, generate
  events, persist any data (images/embeddings/audit trails), or add
  tracking/notifications/network. Real-camera integration and
  operational calibration remain future work.

## [0.11.0] — 2026-10-13 — Phase 1, Step 11: Temporal / multi-frame verification

### Added

* `src/temporal_verification.py`: `TemporalVerifier` — deterministic
  multi-frame temporal verification **on top of** the Step 10 result
  contract (consumes `RecognitionResult` values one at a time; no
  duplication of cosine similarity, thresholds, storage, or manifest
  logic). Evidence policy: only consecutive `MATCHED` frames for the
  **same** employee ID accumulate positive evidence; `UNKNOWN`,
  `AMBIGUOUS`, and `NO_CANDIDATES` never contribute and **break** the
  streak (`INTERRUPTED`, no identity reported); a matched frame for a
  different ID starts a fresh candidate streak (cross-ID evidence is
  never combined). Verification: `VERIFIED(employee_id)` exactly when
  the uninterrupted same-ID streak reaches `min_consistent_frames`
  (frozen `TemporalVerificationResult` with
  `COLLECTING`/`VERIFIED`/`INTERRUPTED` statuses, bounded
  `streak_frames`/`window_frames_used` counters, short reason).
  `reset()` clears all evidence (instance-scoped); one instance per
  tracked face/session — no shared state. Bounded retention only
  (deque capped at `window_frames`); no frames, crops, embeddings,
  scores, or names retained; employee IDs preserved verbatim
  (`"007"`, `"EMP-0042"`). Repeated post-verification frames are
  idempotent state reporting, not recurring verification events.
* `src/config.py`: frozen `TemporalVerificationConfig` +
  `TEMPORAL_VERIFICATION` — `min_consistent_frames=3`,
  `window_frames=10`, both **initial engineering defaults** pending
  Phase 2 real-camera calibration; strict validation (plain ints,
  bools/floats rejected; `window_frames >= min_consistent_frames`).
* `tests/test_temporal_verification.py`: 51 deterministic,
  hardware-free tests covering the temporal checklist (initial state,
  threshold, cross-ID isolation, ID preservation, per-status
  non-evidence, interruption, window bounds, reset & isolation,
  config validation, malformed `MATCHED` refusal, boundaries,
  bounded state, frozen results, and a side-effect-free
  import/construction subprocess check).

### Notes

* Temporal verification is **not** an attendance event; recording
  arrives in Step 13. Deterministic logic only — no real-camera or
  real-recognition-pipeline accuracy validation was performed.

## [0.10.0] — 2026-10-12 — Phase 1, Step 10: Face recognition

### Added

* `src/face_recognition.py`: `FaceRecognitionService` — one reusable,
  injectable recognition decision (crop → Step 08 embedder → Step 09
  storage candidates → deterministic cosine ranking → typed result).
  Decision only — no temporal verification (Step 11), no liveness, no
  unknown-face capture, no attendance events, no camera/writes/
  network. Recognition is a similarity-based decision, never proof of
  identity.
* Similarity & decision: pure cosine `a·b / (‖a‖‖b‖)` in [-1, 1]
  (`cosine_similarity`, unit-testable; validated 1-D/non-empty/
  finite/equal-dimension/non-zero-norm inputs; nothing mutated).
  Deterministic rank by `(-score, employee_id)`; statuses `MATCHED`/
  `UNKNOWN`/`AMBIGUOUS`/`NO_CANDIDATES` in frozen `RecognitionResult`
  (best/runner-up/margin scores + short reason; identity assigned
  only on unambiguous MATCHED). `best == threshold` ⇒ MATCHED;
  `margin == min_margin` ⇒ MATCHED; identical top scores ⇒ AMBIGUOUS
  (ties never resolve into identity). Single candidates decided on
  threshold alone.
* `src/config.py`: frozen `RecognitionConfig` + `RECOGNITION`
  — `min_similarity=0.36` (raw-cosine scale) and `min_margin=None`,
  both **explicitly provisional** until empirical calibration; range
  validation in `__post_init__`. The literature 0.6 threshold
  (normalized features) is deliberately not reused, because Step 08
  stores raw crop-only embeddings; `FaceProcessingConfig` unchanged.
* Employee eligibility via existing authorities only: the Step 07
  published enrollment manifest gates candidacy (no second source of
  truth); active status trusted from the manifest (or overridden by
  an injectable `active_lookup`); untrusted manifests are validated —
  unreadable/malformed/schema-mismatched/identity-contradicting ones
  raise the new typed `ManifestIntegrityError` (fail-closed default,
  documented `"skip"` escape hatch). Inactive employees are excluded
  from decisions but their records are never deleted. Stored IDs
  (`007`, `EMP-0042`) are preserved exactly.
* `src/enrollment.py`: backward-compatible public `safe_dir_name()`
  wrapper so the recognizer reuses the writer's exact encoded-name
  scheme (no duplication, no behavior change).
* Corruption & failure honesty: corrupt records surfaced via explicit
  candidates raise their typed Step 09 errors; a vanished record
  raises `RecognitionStorageError`; embedder failures raise
  `RecognitionModelError`; invalid inputs/config raise
  `RecognitionInputError`. Results/logs never contain crops or full
  vectors (privacy by contract, tested).
* `tests/test_face_recognition.py`: 74 deterministic tests on fake
  embedders + temp-dir real storage (no real SFace artifact, no
  camera, no network, no real biometrics) covering: matches, explicit
  subsets, unknown, exact threshold/margin boundaries, ambiguity,
  ties, candidate-set edges, vector validation matrix (NaN/inf/zero-
  norm/empty/dimension mismatch), corrupt-record propagation,
  embedder/storage failures, config validation, ID preservation,
  active/inactive + manifest integrity, model-compatibility honesty,
  immutability, privacy, import/construction/foreign-CWD/subprocess
  hygiene.

### Verified

* Focused recognition tests: 74 passed.
* `python -m pytest` → **495 passed** (421 prior + 74 new) from the
  project root and an external working directory; stable across
  repeated runs.
* `git diff --check` → clean; no biometric artifacts in the repo.

### Known limitations

* **Real SFace inference remains unverified** (artifact absent,
  never downloaded; Steps 08/09 status carried forward) — all
  recognition tests are fake-backed with real validation rigor, and
  no recognition accuracy claim is made.
* Model identity is not machine-enforced (no identifier exists in
  Step 09 sidecars and none was invented): all candidates in one
  deployment must come from one known, consistent embedding-model
  generation (documented operational remedy; no format change).
* Thresholds/margin are provisional starting points on the raw
  cosine scale; Phase 2 calibration required before production use.
* Not included per plan: temporal verification (Step 11), liveness,
  unknown-face capture, attendance events, camera loop.

## [0.9.0] — 2026-10-12 — Phase 1, Step 09: Embedding storage

### Added

* `src/embedding_storage.py`: `EmbeddingStorageService` — saves,
  loads, lists, and validates stored face embeddings under the
  existing configured `data/embeddings/` directory. Storage only: no
  matching (Step 10), no recognition, no attendance, no enrollment
  changes; storage proves nothing about identity.
* Storage format (one choice, documented): per employee one NumPy
  `.npz` array file (`np.savez` write / `np.load(allow_pickle=False)`
  read — no pickle, no object arrays) plus a JSON metadata sidecar
  (schema `format_version: 1`: exact employee ID, declared dimension,
  `float32` dtype marker, timezone-aware UTC `stored_at`). Record
  name stems reuse Step 07's traversal-proof percent-encoding; IDs
  (`007`, `EMP-0042`) survive byte-exactly and must satisfy the
  `Employee` model rules. No database, no new dependency.
* Contracts: `save(employee_id, embedding, *, replace=False)` accepts
  a Step 08 `FaceEmbedding` or a raw 1-D float array (normalized to
  the float32 on-disk contract); `load()` re-validates everything on
  every call treating stored files as untrusted input;
  `exists()` counts only complete record pairs;
  `list_employee_ids()` skips corrupt sidecars instead of crashing.
  The constructor's `expected_dimension` (default `0` = any positive
  dimension) is enforced on both save and load, making cross-model
  dimension compatibility explicit (`EmbeddingRecordFormatError`).
* Safety: save validates the vector (1-D, non-empty, floating, finite,
  non-zero norm) and the ID **before any write**; a record is never
  overwritten without the explicit `replace=True` flag (files staged,
  flushed, fsync-ed; hard-link publication fails loudly on a
  concurrent winner; publication order array-then-sidecar so a crash
  can leave an unpublished array but never a metadata-declared record
  without its validated sidecar); failed attempts clean only their
  own staged/unpublished artifacts. Typed errors:
  `EmbeddingStorageInputError` (ValueError),
  `DuplicateEmbeddingRecordError`, `EmbeddingRecordNotFoundError`,
  `CorruptEmbeddingRecordError`, `EmbeddingRecordFormatError`,
  `EmbeddingPersistenceError`. Returned records are frozen with
  write-protected vectors. Import/construction perform no filesystem
  work (directories created only when a real save needs them).
* `tests/test_embedding_storage.py`: 79 deterministic tests on
  synthetic vectors in temporary directories (no real biometrics, no
  network): round trips, exact-ID preservation, vector validation
  (NaN/±inf/zero-norm/shape/dtype), duplicate refusal + explicit
  replace, corruption matrix (garbage files, invalid JSON, object
  arrays, smuggled pickle bytes that must never execute, on-disk
  NaN, dtype/shape/dimension/ID mismatches, naive timestamps,
  unknown dtype markers, incompatible schema versions), traversal
  and malicious-ID rejection before any write, write-failure cleanup
  with unrelated data untouched, hygiene (import/construction/
  foreign-CWD/fresh-subprocess/no-device-no-network), and output
  isolation.

### Verified

* Focused embedding-storage tests: 79 passed.
* `python -m pytest` → **421 passed** (342 prior + 79 new) from the
  project root and an external working directory; stable across
  repeated runs.
* `git diff --check` → clean.
* No biometric artifacts in the repository (tests use temp dirs).

### Known limitations

* Storage does not consult enrollment manifests (deliberate — no
  second identity source-of-truth); recognizing only enrolled
  employees is Step 10's gating decision.
* Crash between the two file publications leaves an orphan array
  (never a declared record without metadata); orphans are skipped by
  `exists()`/`list_employee_ids()` but not garbage-collected.
* Model identity/version is not recorded (SFace exposes no version
  API); dimension compatibility is the explicit contract instead.
* Not included per plan: matching/recognition (Step 10), temporal
  verification, unknown-face handling, attendance logic.

## [0.8.0] — 2026-10-11 — Phase 1, Step 08: Face embedding generation

### Added

* `src/face_embedding.py`: `FaceEmbedder` — converts one BGR uint8
  face crop (the exact format Step 07 persists) into a validated
  float32 1-D embedding. Explicit `load()`/`is_loaded`/`embed()`
  lifecycle; import and construction are side-effect-free;
  `backend_factory` injectable (tests use fakes; the real default is
  `cv2.FaceRecognizerSF`, bundled in the pinned opencv-python wheel —
  no new dependency, no `onnxruntime`). Typed errors:
  `FaceEmbeddingInputError` (ValueError) / `FaceEmbeddingModelError`
  (RuntimeError) / `FaceEmbeddingOutputError` (RuntimeError).
  Validated frozen `FaceEmbedding` result: write-protected vector,
  dimension, pre-normalization L2 norm, explicit dtype marker.
* Output contract: 1-D, finite, dimension validated against the
  configured `expected_dimension` (0 = accept any non-empty),
  zero-norm rejected (never divided), L2 normalization only when
  `normalize_output=True` (contract-driven, not convention).
* `tests/test_face_embedding.py`: 33 deterministic tests on
  injected fakes — input contract, lifecycle, generation/output
  validation (NaN/inf/zero-norm/shape), output isolation,
  hygiene (import/construction/foreign-CWD/fresh-subprocess), and
  Step 07 PNG-crop format roundtrip compatibility.

### Model approach

* **SFace** (OpenCV Zoo `face_recognition_sface_2021dec.onnx`) chosen
  over insightface buffalo: buffalo's ArcFace path requires five
  landmarks that Step 07 crops do not carry; SFace's documented
  crop→112×112 preprocessing works on plain crops; Apache-2.0 (vs
  research-only buffalo); bundled DNN loader in the pinned wheel.
* The SFace artifact is **operator-supplied** at `models/sface.onnx`
  and **never downloaded**; until provided, `load()` raises
  `FaceEmbeddingModelError`. **Real-model inference is pending**
  (no artifact in this repository; unit tests are explicitly
  fake-backed).

### Verified

* Focused embedding tests: 33 passed.
* `python -m pytest` → **342 passed** (309 prior + 33 new) from the
  project root and an external working directory; stable across
  repeated runs.
* `git diff --check` → clean.

### Known limitations

* Crop-only (no landmark alignment) SFace accuracy is unmeasured;
  Phase 2 evaluation on real footage required before any accuracy
  claim.
* Not included per plan: embedding persistence (Step 09),
  matching/recognition (Step 10), liveness, attendance logic.

## [0.7.0] — 2026-10-11 — Phase 1, Step 07: Multi-image face enrollment

### Added

* `src/enrollment.py`: `EnrollmentService` — one controlled, bounded
  capture session per employee: identity validation via the existing
  `Employee` model (IDs preserved exactly, `007`/`EMP-0042`),
  duplicate-refusal before any capture, camera/detector/assessor
  orchestration through the existing Step 04–06 services, per-sample
  accept/reject with explicit typed reasons, and staged persistence.
  Typed errors: `EnrollmentInputError`, `DuplicateEnrollmentError`,
  `EnrollmentPersistenceError`. Results: frozen `EnrollmentResult` with
  COMPLETED/CANCELLED/FAILED status, accepted `SampleRecord`s,
  per-frame `SampleRejection`s (reason + measured quality), attempt
  count and duplicate-skip count. Camera released on every exit path;
  cancellation and failure never persist; persistence failures clean
  up only current-attempt artifacts. Camera, detector, assessor,
  config, and settings are injectable (hardware-free tests).
* `src/config.py`: `EnrollmentConfig.max_capture_attempts=60` — the
  bound preventing endless capture loops when frames keep failing;
  validated `>= min_samples`.
* Storage layout (both roots already git-ignored):
  `<data>/employees/<encoded-id>/employee.json` (schema version 1;
  full Employee serialization incl. `is_active`, sample count,
  per-sample file/UTC time/quality metrics) and
  `<data>/face_samples/<encoded-id>/sample_001.png`. `<encoded-id>` is
  a deterministic percent-encoding of the exact ID (dots encoded),
  making path traversal structurally impossible. Existing enrollments
  are never overwritten; samples stage in `.staging-` dirs, are
  verified by re-decoding, and publish samples-first,
  manifest-last.
* `tests/test_enrollment.py`: 38 deterministic tests with fakes and
  synthetic frames — validation, duplicate refusal, capture flow,
  cancellation, camera lifecycle/errors, service exceptions, attempt
  budget, byte-duplicate skipping, persistence layout/content,
  path-safety encoding, failure cleanup, and import hygiene.

### Verified

* Focused enrollment tests: 38 passed.
* `python -m pytest` → **309 passed** (271 prior + 38 new) from the
  project root and an external working directory; stable across
  repeated runs.
* Fresh-interpreter/foreign-CWD hygiene: import and service
  construction create no directories and touch no camera/model/files.
* `git diff --check` → clean.

### Known limitations

* Near-duplicate biometric similarity is out of scope (needs
  embeddings, Step 08); only exact byte-duplicate suppression exists.
* Multi-file persistence is best-effort atomic, not transactional: a
  crash between sample and manifest publication can leave orphan
  sample files — never a declared enrollment without images.
* Capture/sampling thresholds are initial defaults; Phase 2
  calibration pending.

### Not included (per plan — later steps)

* Embeddings (Step 08), recognition (Step 10), liveness, attendance
  logic, CLI/GUI enrollment UX.

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
