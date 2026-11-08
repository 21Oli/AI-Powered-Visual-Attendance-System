# DEVELOPMENT_STATUS.md

What is actually completed, in progress, blocked, or not started.
Updated after every verified step — never ahead of implementation.

## Step 38 — Phase 3 G2: startup schema compatibility gate (2026-11-08)

**Implemented and offline-verified. No application database was contacted and
no migration was executed; live startup verification is not claimed.**

* **Added `src/startup_schema.py`** (the gate): both DSNs are mandatory;
  targets are normalized through the existing `src/db_target.py` and an
  ambiguous, malformed, missing, or identical target is refused before any
  connection; only the application DSN is ever opened. Policy reuses
  `read_state()`/`_reconcile()` and adds only the G2 rules the loader treats
  as benign — non-empty shipped set, present ledger, set beginning at V001,
  nothing pending. Refusals are a typed `StartupSchemaError` with one of the
  13 approved stable codes and a fixed sanitized message; the cause stays on
  `__cause__` for trusted diagnostics only.
* **Added `PsycopgMigrationExecutor.verify_schema_locked()`**: the one-connection
  operation the design requires. Connect → `pg_try_advisory_lock` (nonblocking,
  no retry) → `REPEATABLE READ READ ONLY` snapshot plus the existing
  ledger-shape query → evaluation while the lock is held →
  `pg_advisory_unlock` confirmed by its boolean → close. A refused lock raises
  `MigrationContentionError`; an unconfirmed/erroring unlock or a close failure
  raises `MigrationCleanupError`; a refusal in flight is preserved with the
  cleanup failure attached as a note. The six-method ABC, `read_snapshot()`'s
  ownership contract, and the migration CLI are unchanged; the snapshot SQL is
  shared through one private helper.
* **Wired `src/api.py::run_server(..., startup_verifier=None)`**: guard → G2 →
  Uvicorn, so a refusal releases the guard and never imports or constructs a
  server. Default is the real verifier; the keyword is the approved test seam
  (no environment bypass, no warning-only mode). `create_app()` and module
  import stay database-free.
* **`tools/run_api.py`**: G2 refusal → one sanitized stderr line and exit `4`;
  `--print-bind` still skips the gate; exits `0`/`2`/`3` unchanged.
* **Tests:** new `tests/test_startup_schema.py` and
  `tests/test_api_startup_gate.py` (55 offline tests: policy table, target
  safety, one-connection order, no snapshot after a refused lock, no retry,
  cleanup matrix, guard/verification/Uvicorn order, guard release on refusal,
  no-bypass default, runner `0`/`3`/`4` with sanitized stderr).
  `tests/test_api_guard.py`'s real-server cases inject the no-op verifier so
  the API suite stays database-free, and `tests/test_api.py`'s driver tripwire
  now checks the running test's own imports (the G2 target check legitimately
  uses psycopg's DSN parser without connecting; the fresh-subprocess import
  test still proves `import src.api` pulls no driver).
* **Verification:** focused `tests/test_api.py tests/test_startup_schema.py
  tests/test_api_startup_gate.py tests/test_api_guard.py` → **107 passed**;
  full offline suite `pytest -q` → **1705 passed, 32 skipped in 219.93s**
  (exit 0), the 32 skips being the unchanged opt-in Tier-2 modules.
* **Safety evidence:** no `ATTENDANCE_DATABASE_URL` connection, no migration,
  no DDL/DML, no dependency change, no configuration-value change, and no Git
  operation. Existing work was preserved (the tree was clean at the start of
  this step).
* **Limitations / not claimed:** the gate is offline-contract-verified only.
  A read-only live check against the application database (documented
  procedure in `DB_DESIGN.md` §11.8) has **not** run, so startup behavior on a
  real ledger is not verified. Physical schema drift remains outside G2.
* **Docs:** `DB_DESIGN.md` §3.2, §3.3, §3.7 (G2 gap retired), new §3.8, §6
  row 1.5b-7, new §11.8; `README.md` (exit `4`, read-only startup
  verification, apply-before-serve); `CHANGELOG.md`; `.env.example`.

## Step 37 — Phase 3 Step 1.5b-6: real migration executor and migration CLI (2026-11-07)

**Implemented, offline-verified, and read/lock-verified live. No migration
has been executed or committed against any database.**

* **Added `src/db_migrate_executor.py`** (`PsycopgMigrationExecutor`): the
  six-method runner contract over real psycopg connections, plus
  `read_snapshot()` — one `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`
  transaction that reads ledger presence, the public table list and (only
  when that snapshot reported the ledger present) its rows, then rolls back
  and closes. Explicit non-blank DSN required (no application fallback);
  session-scoped nonblocking advisory lock on a dedicated connection held
  for the whole run; the ledger is created only when `to_regclass` says it
  is absent, otherwise its documented column shape is validated and a
  divergence is a typed `MigrationIntegrityError`; each migration's SQL runs
  verbatim (parameterless) with its ledger insert in one transaction and one
  commit, rolling back on any failure. Availability/DSN errors propagate
  unchanged; unexpected driver failures become sanitized
  `MigrationDriverError` with `__cause__` preserved.
* **Added `src/db_target.py`** (`normalized_target_identity`, `same_target`):
  dbname/host/normalized-port identity, local host aliases collapsed,
  user/password excluded, and `hostaddr`/`service`/missing host/missing
  dbname reported as ambiguous rather than assumed.
* **Added `MigrationSnapshot`, `MigrationState`, `read_state()`** to
  `src/db_migrate.py` (append-only): `migrate()`, `_reconcile()`, checksum
  logic, script validation, `ADVISORY_LOCK_KEY`, V001, the six-method ABC
  and `FakeExecutor` are untouched. `read_state()` is pure and delegates
  every judgement to the existing `load_migrations()`/`_reconcile()` rules.
* **Added `tools/run_migrations.py`** (`status`, `status --json`, `apply`)
  with the documented exit-code map (0/1/2/3/4/5/6), credential-free
  `dbname=… host=… port=…` output, and fail-closed target rules: it requires
  `ATTENDANCE_DATABASE_URL`, refuses a target matching the configured test
  DSN after normalization, and refuses (exit 2) when a configured test DSN
  cannot be normalized. `status` never creates the ledger, never takes the
  lock, and performs no write.
* **Offline tests:** `tests/test_db_target.py`,
  `tests/test_db_migrate_executor.py`, `tests/test_run_migrations_cli.py` —
  **82 passed** in the focused run. Full offline suite: **1629 passed, 31
  skipped** (`1629 passed, 31 skipped in 225.87s`, exit 0). The 31 skips are
  the two opt-in Tier-2 modules (17 pre-existing + 14 new), so no previously
  passing test started skipping and no new skip appears in the offline run.
* **Live Tier-2, read/lock only:** `tests/test_tier2_migration_executor.py`
  with `tests/tier2_support.py`, run as
  `ATTENDANCE_TIER2_OPT_IN=1 ./.venv/Scripts/python.exe -m pytest tests/test_tier2_migration_executor.py -m tier2 -v`
  → **14 passed, 0 failed, 0 skipped**. Target: `visual_attendance_tier2_test`
  on `localhost:5432` only, through the guard's verified target and a DSN
  bound to it with `bind_target_to_dsn`; `ATTENDANCE_DATABASE_URL` was never
  connected to. The unmodified guard reported `READY` (a block would have
  skipped with its typed reason, not passed).
* **Safety evidence:** catalog inventory (relations, columns, constraints,
  indexes, ledger rows, employee count, guard rows) was identical before and
  after every test and at the module baseline; no DDL/DML was committed, no
  employee/ledger/guard row changed, no cleanup delete ran, and no migration
  was committed. The single write attempt was a deliberately failing
  `SELECT 1 / 0` whose transaction rolled back and left no `version = 999`
  row.
* **Limitations:** G2 (no startup enforcement), G3 (migration files are
  validated, not hardened), G7 (cleanup non-masking covers ordinary
  `Exception`s; a non-`Exception` `BaseException` is re-raised untouched) —
  DB_DESIGN.md Section 3.7. `ensure_ledger()`'s CREATE path and `apply`
  against a live schema remain fake-verified only.
* **Docs:** `DB_DESIGN.md` §3.4 (snapshot semantics, CLI, exit codes), new
  §3.7 (gaps), §3.6 (OQ-1/OQ-4 status), §5.2 (live status), §6 (row and
  baseline), §11.6/§11.7; `README.md` (migration CLI); `CHANGELOG.md`;
  `.env.example` (test database name/role, opt-in scope).
* **Unchanged:** no dependency change, no runtime-wiring change, and no Git
  operation was performed.

## Step 36 — Phase 3 Step 1.5c-3b: live Tier-2 PostgreSQL verification executed (2026-11-07)

**Executed against the provisioned dedicated test database; live
verification distinguishes from, and does not replace, the offline suite.**
The owner completed the reviewed one-time provisioning script; the guard
preflight then reported `READY` and the opt-in harness was run for the
first time against real PostgreSQL.

* **Target:** database `visual_attendance_tier2_test` on `localhost:5432`,
connected only through `ATTENDANCE_TEST_DATABASE_URL` as the
least-privilege role `attendance_tier2_runtime`. `ATTENDANCE_DATABASE_URL`
was never connected to; it reached only the guard's distinctness proof.
* **Opt-in isolation:** `ATTENDANCE_TIER2_OPT_IN` is no longer in `.env`,
and `load_settings()` was observed to leave `os.environ` without it, so the
switch cannot be inherited from the file. It was set to the exact value
`1` inline for the single documented invocation below and was not written
to `.env` or any other file.
* **Preflight (read-only, unmodified `src/test_database_guard.py`):**
`RESULT: READY` — declared and connected identity matched the test target
and differed from the application's, server-side read-only was enforced
before any verification query, the `_phase3_test_guard` marker had exactly
one row with the approved literal and a timezone-aware `created_at`, and
the marker's claimed `schema_version` (1) matched the migration ledger's
recorded version (1).
* **Exact command and scope:**

  ```bash
  ATTENDANCE_TIER2_OPT_IN=1 ./.venv/Scripts/python.exe -m pytest \
      tests/test_tier2_employee_repository.py -m tier2 -v
  ```

  **13 passed, 0 failed, 0 skipped, 0 errors** (`13 passed in 12.02s`).
  Because nothing skipped, every guarded path ran live: real connection
  open/close, PostgreSQL refusing a write in a read-only transaction,
  dry-run classification, `--apply` creating the synthetic employee,
  identical repeat, each differing field (`full_name`, `is_active`,
  `created_at`) as a never-updated conflict, duplicate create →
  `EmployeeAlreadyExists`, create race → conflict, failed transaction
  leaving no state with a reusable connection, typed not-found on update
  without writing, and the spy proving the importer never updates or
  deactivates.
* **Cleanup:** guarded cleanup ran for the run's own synthetic rows and
  left **zero** behind — the teardown asserts this and would have failed
  the run otherwise. The harness's per-run `deleted`/`absent` counters are
  surfaced only through `report.left`, so **separate deleted/absent counts
  are not exposed and are not claimed here**. A post-run read-only count
  confirmed `public.employee` at 0 rows total, 0 rows in the `T2-`
  namespace, and the ledger and marker unchanged at one row each.
* **Not verified, and not claimed:** production readiness, any real-camera
  path, real employee data, photos, embeddings, or attendance records.
  Only the disposable test database and its synthetic namespace were
  touched, and no application-database connection was opened.
* **Unchanged by this step:** no production source, schema, migration,
  dependency, or configuration file was modified; the harness, the guard,
  and the repository wiring ran exactly as committed. The earlier
  exposure of the test role's password in a pytest assertion still stands
  as a rotation recommendation.

## Step 35 — Phase 3 Step 1.5c-3b-prep: Tier-2 PostgreSQL verification harness prepared (2026-11-05)

**Harness prepared and verified offline; not executed.** No PostgreSQL
connection was opened, no DSN value was read, and no Tier-2 scenario ran:
the committed baseline (branch `main`, HEAD `1692cb9`) required the
dedicated test database, which is still owner-provisioning territory.

* **`tests/test_tier2_employee_repository.py` (new):** the opt-in Tier-2
harness. It cannot run in the ordinary offline suite: without
`ATTENDANCE_TIER2_OPT_IN=1` every item skips itself at collection with a
reason naming the switch (proven by a fresh-subprocess run), and a skip is
never reported as a pass. With the opt-in on, the module still refuses to
start unless both `ATTENDANCE_TEST_DATABASE_URL` and
`ATTENDANCE_DATABASE_URL` are configured (pre-flight, presence-only,
before any opener), and then hands both DSNs to the existing
unmodified `src.test_database_guard.verify_test_database` — identity
declaration, real connection, read-only enforcement, connected identity,
marker literal, marker/ledger agreement. Any blocked outcome skips with
the guard's own typed reason code; the harness never weakens, bypasses,
or re-implements a guard check.
* **Targeting discipline:** every repository is built explicitly via
`open_employee_repository(dsn=<test DSN>, read_only=…)`. The wiring's
default resolution would read the application DSN, so the default is
never used; the application DSN reaches only the guard, for its
distinctness proof. No connection opens on a missing test DSN.
* **Live scenarios (all opt-in):** real connection opens/closes/answers;
PostgreSQL refusing a write in a read-only transaction (asserted via
`sqlstate == "25006"`, no row created); dry-run classification against a
read-only repository; `--apply` creating the synthetic employee;
repeated import identical; each differing field (`full_name`,
`is_active`, `created_at`) a conflict that never updates; duplicate
direct create → `EmployeeAlreadyExists`; a create race (miss-then-create
wrapper) reported as a conflict; rollback/cleanup on failure leaving a
reusable connection; and a spy repository proving the importer never
reaches `update_full_name`/`deactivate`/`list`.
* **Synthetic rows:** every row uses a per-run namespace
(`T2-<8-hex-run>-<slot>` with a synthetic name and fixed timestamp), a
fixture records each intended row, and teardown runs guarded cleanup.
* **Guarded cleanup:** the harness's only write is
`cleanup_synthetic_employees` — it refuses without a guard-produced
`VerifiedTestTarget`, requires the single authorized action against an
explicit one-entry manifest via
`src.test_database_guard.require_destructive_action` **before**
connecting, accepts only harness-shaped IDs, re-reads each candidate and
deletes only rows whose stored tuple equals exactly what the run wrote,
uses one bound-parameter `DELETE … = ANY(%s)` (no `LIKE`, no prefix
match, no DDL), reports `deleted`/`absent`/`left`, and closes the
connection on success and failure. Records that cannot be positively
claimed are left untouched.
* **`tests/test_tier2_harness_control.py` (new):** 25 offline harness-
control tests, all fake-driven (a genuine unforgeable `VerifiedTestTarget`
is produced through the real guard with an injected connection and a
stdlib DSN parser, keeping the shared pytest process psycopg-free):
opt-in semantics, pre-flight ordering (opt-in → test DSN → app DSN,
verifier/openers never run on a missing input), skip-with-typed-reason,
repository built only with the explicit test DSN (application DSN only
ever reaches the guard), no direct `VerifiedTestTarget` construction,
cleanup manifest/ownership/shape gating, exact-match-only deletion
(sibling prefix IDs never eligible), connection closed on success and
failure, exact statement/parameter audit, and an AST scan proving the
module's SQL is exactly the two bound-parameter statements — no CREATE,
ALTER, TRUNCATE, INSERT, or UPDATE anywhere in any string literal.
* **`pytest.ini`:** registered the `tier2` marker so opt-in skips are
explicit configuration, not unknown-mark noise.
* **`CHANGELOG.md`, `DB_DESIGN.md` §11.6, `.env.example`:** documented
the prepared harness, the opt-in switch, and what owner provisioning
must supply before a real run.

**Owner provisioning (required before any Tier-2 run):** provision a
dedicated, disposable local PostgreSQL test database; establish the
`_phase3_test_guard` marker row with the
`VISUAL_ATTENDANCE_PHASE3_TEST_ONLY` literal (per DB_DESIGN §5.3) with a
timezone-aware `created_at` and a `schema_version` equal to the applied
migration ledger's `MAX(version)`; set `ATTENDANCE_TEST_DATABASE_URL`,
`ATTENDANCE_DATABASE_URL`, and `ATTENDANCE_TIER2_OPT_IN=1` in the shell
that runs the harness. Nothing in this step licenses pointing any
variable at a shared or production database.

**Not verified, and not claimed:** everything that needs PostgreSQL —
server-side read-only refusal, real connection lifecycle, marker
evidence, the import against a real database, and the guarded cleanup
against real rows. The harness is prepared but has never executed.

## Step 34 — Phase 3 Step 1.5c-3a: PostgreSQL employee-repository wiring (2026-11-04)

**Wiring implemented and verified offline; no PostgreSQL connection was
opened and no real import ran.** Baseline observed at start: branch
`main`, HEAD `120a497` (the owner had committed Step 1.5c-2), clean tree,
nothing staged.

* **`src/db_employee_repository.py` (new):** production wiring that binds
the approved `EmployeeRepository` to `src.db.open_connection`. It defines
**no SQL**, no second persistence implementation, no retry and no
fallback source — statement ownership and typed errors stay in the
repository. `open_employee_repository(dsn=None, *, read_only=False,
connection_opener=None)` returns the repository **without opening a
connection**; the first repository call connects. `src.db` (and
psycopg) is imported lazily inside the default opener, so importing the
module loads no driver and reads no DSN.
* **Lifecycle (one operation = one connection):** open → (read-only
  requested and read back before any statement, when
  `read_only=True`) → the repository's single `execute` → `commit()` on
  success → `rollback()` + re-raise the *same* exception on failure →
  `close()` on every path. A rollback error is swallowed while another
  exception is propagating; a close error is attached as an `add_note`
  note instead of replacing the original, and is raised when nothing else
  failed. Read-only that cannot be set or does not report back is refused
  (`EmployeeRepositoryWiringError`, fail closed). Cursors stay
  driver-owned and are released with the connection.
* **`tools/import_employee_metadata.py` (changed):** repository-backed
  modes now build the production wiring lazily via
  `_default_repository_factory(apply=...)` →
  `open_employee_repository(read_only=not apply)`. `--validate-only`
  never imports the wiring (`--apply` help text and mode text updated).
  The runner keeps a defensive refusal (exit 2, no database access) for a
  direct library call with no repository at all. No DSN, driver message,
  employee name or report content is printed; a stopped run still reports
  only the exception **type name**.
* **Testing (offline only):** new `tests/test_db_employee_repository.py`
  → **38 passed**: an in-memory psycopg-3-shaped fake server (rows,
  `sqlstate`-carrying violations, settable/readable `read_only`,
  observable commit/rollback/close) drives the **real** repository SQL —
  construction opens nothing, `get`/`create` bind the approved
  parameters, `23505` becomes `EmployeeAlreadyExists` while `23502` and
  a sqlstate-less error do not, read-only is requested **before** the
  first statement, writable repositories never request it, every failure
  path rolls back and closes (including rollback- and close-failure
  masking), availability errors propagate unchanged, the DSN reaches the
  opener verbatim while `None` stays the connection layer's business,
  non-boolean `read_only` is refused, the module defines no SQL and
  imports no driver, and a fresh-process check proves importing it loads
  neither `src.db` nor psycopg. The importer is then run over that wiring
  (dry run reads and never inserts; `--apply` inserts only missing rows
  with the artifact's `created_at`; conflicts and create races never
  write; an unavailable database stops the run and the report contains
  only the exception type).
* **`tests/test_employee_import.py` (changed):** **96 → 102 passed** — 6
  new tests that `--validate-only` never builds the wiring, dry run
  requests `read_only=True`, `--apply` requests `read_only=False`, an
  injected factory wins over production, the mode→read-only mapping is
  exact, and importing the CLI loads neither the wiring nor psycopg. Two
  obsolete tests were **renamed and rescoped, not weakened**: the former
  "missing wiring is refused" cases now assert the runner's defensive
  no-repository refusal (exit 2, no repository call), because the CLI now
  always supplies the production wiring.
* **Verification:** focused `pytest tests/test_db_employee_repository.py
  tests/test_employee_import.py -q` → **140 passed**; repository +
  exporter + migration + test-database guard + API + `src.db` suites →
  **263 passed, 4 skipped**; full offline sweep (camera /
  enrollment-CLI / YUNET-download suites excluded) → **1522 passed, 0
  failed, 4 skipped, exit 0**, against a **1478** baseline at `120a497`
  (delta exactly the 44 new tests). The 4 skips are the exporter's
  platform-limited symlink/permission cases. `git diff --check` clean.
  Nothing staged or committed.
* **Deliberately NOT verified (and not claimed):** that PostgreSQL
  actually refuses a write in the read-only transaction (the fake cannot
  model server-side enforcement), real psycopg connect/commit/close
  behaviour, DSN resolution from a live environment, migrations, and the
  Tier-2 real import. Those require the separately authorized Step 1.5c-3
  run against the owner-provisioned dedicated test database.
* **Not changed:** `requirements.txt` / dependencies, `.env.example`,
  `src/config.py`, the repository, migration runner, guard, API,
  recognition, enrollment, and attendance behaviour.

## Step 33 — Phase 3 Step 1.5c-2: offline employee-metadata importer (2026-11-03)

**Importer implemented against the Step 1.5c-1 artifact; no database was
contacted and no real import has run.** The owner supplied the exact
reconciliation policy (create-if-absent, skip-if-identical,
report-conflict-without-updating, reject invalid/duplicate records,
stop-on-operational-error); this step implements it with an injected
repository and verifies it entirely offline. Baseline observed at start:
branch `main`, HEAD `ed7ef99` (the owner had committed Step 1.5c-1),
clean tree, nothing staged; all 1.5c-1 work is preserved.

* **`src/employee_import.py` (new):** strict JSONL validator plus
  reconciler over the five-key artifact contract — JSON object only, all
  five keys and no others, integer `schema_version` 1, employee
  ID/name validated **through the `Employee` model** with any
  normalization change rejected, real bool `is_active`, tz-aware
  ISO-8601 `created_at` preserved as supplied (never import time).
  Typed rejections (12 codes) carry line number and readable ID only.
  Duplicate IDs inside the artifact reject **every** occurrence. The
  repository surface is narrowed to a two-method
  `EmployeeRepositoryLike` protocol (`get`, `create`): no update, no
  deactivation, no listing path exists, so a conflict cannot become a
  write and `created_at` cannot be modified. `EmployeeAlreadyExists`
  during a create is a conflict with no retry; any other repository
  exception stops the run immediately, recording only the exception's
  **type name** (never its text). Records are processed in
  `employee_id` order, so import order does not depend on file order.
* **Modes:** `validate-only` (no repository, no database),
  `dry-run` (classifies identical / would-create / conflict without
  writing) and `apply` (approved creates only). Reports are JSON with
  counts, employee IDs, differing field **names**, line numbers and
  reason codes — no names, timestamps, record contents, DSNs or driver
  text.
* **`tools/import_employee_metadata.py` (new):** operator CLI —
  `--input` (defaults to `<DATA_DIR>/exports/employee_metadata.jsonl`),
  `--validate-only`, `--apply`, `--report`, `--overwrite-report`. Exit
  codes `0` clean, `1` rejections/conflicts/stopped/report failure,
  `2` CLI or configuration refusal. Because the psycopg-backed runtime
  wiring is **Step 1.5c-3's** deliverable and explicitly out of scope,
  the CLI refuses both repository-backed modes (default dry run and
  `--apply`) with exit 2 *before any database access*, directing the
  operator to `--validate-only`. Reports are written atomically and an
  existing report is never replaced without `--overwrite-report`
  (refused before any work starts).
* **Testing (offline only):** new `tests/test_employee_import.py` →
  **96 passed** (no skips). Coverage: dry run performs only `get` calls
  and mutates nothing; `--apply` creates missing employees with the
  artifact's `created_at` instant; identical rows are skipped; a
  difference in each field (`full_name`, `is_active`, `created_at`) is a
  conflict with the differing field names and zero mutation; an
  `EmployeeAlreadyExists` race is a conflict with a single `create`
  attempt and no retry; invalid records are rejected while valid ones
  continue; duplicate IDs reject every occurrence and import nothing;
  unsupported schema versions, missing/extra keys, malformed JSON,
  non-UTF-8 input, invalid ID/name/status/timestamp, and
  normalization-changing values all reject with typed codes; repository
  errors stop the run safely (including after a successful create) with
  no driver text in the report; reports contain no names or timestamps;
  report creation/refusal/overwrite/atomic-failure behavior; every CLI
  exit code and refusal path, including the missing-wiring refusal
  making no repository call; and a behavioral proof that the importer
  never touches `update_full_name`, `deactivate`, or `list` (the fake
  records and raises on any such call).
* **Verification:** focused `pytest tests/test_employee_import.py -q` →
  **96 passed**; importer + repository + exporter suites → **215
  passed, 4 skipped** (the 4 skips are the exporter's platform-limited
  symlink/permission cases); full offline sweep (camera /
  enrollment-CLI / YUNET-download suites excluded) → **1478 passed, 0
  failed, 4 skipped, exit 0**, against a **1382** baseline at `ed7ef99`
  (delta exactly the 96 new tests); `git diff --check` clean. Nothing
  staged or committed.
* **Deliberately NOT in this step:** the psycopg-backed repository
  wiring, any DSN read, connection, SQL, migration or Tier-2 run (Step
  1.5c-3); the real import of real employee metadata; any API endpoint
  or recognition change. Never-enrolled and deleted employees remain
  outside the population, and the manifest-vs-database active-status
  divergence remains documented, not resolved.

## Step 32 — Phase 3 Step 1.5c-1: employee-metadata export from enrollment manifests (2026-11-03)

**Owner-approved source decision, export half only — nothing is
imported.** Step 1.5c asked whether a safe employee-metadata source
exists. The investigation (read-only, recorded in Step 1.5c's design
report) found exactly one: the Step 07 enrollment manifest
`<DATA_DIR>/employees/<encoded-id>/employee.json`, `format_version: 1`,
whose `employee` section is the `Employee.to_dict()` payload. There is
**no employee-metadata JSONL** anywhere in the project (the only JSONL is
the attendance journal — five keys, employee ID only), and the embedding
sidecars carry no name or status. Approved: manifest as the source, a
versioned five-key JSONL artifact as the export, the enrollment
`created_at` preserved verbatim, and the artifact at
`<DATA_DIR>/exports/employee_metadata.jsonl` (git-ignored; personal
data).

* **`src/employee_export.py` (new):** read-only scan of the manifest
  root. Only `employee.json` files that are direct children of the root
  are opened; symlinked entries, symlinked manifests, and a symlinked
  root are refused (`symlink_refused`), so the scan cannot leave the
  configured root. Nothing under `face_samples/`, `embeddings/`, or
  `attendance/` is opened, and the `samples` array / `sample_count` of a
  manifest are never exported.
* **Validation, all typed (`ExportRejection`, 17 codes):**
  `not_a_directory`, `symlink_refused`, `manifest_missing`,
  `manifest_unreadable` (non-UTF-8/unreadable), `malformed_json`,
  `unsupported_format_version` (only integer `1`),
  `missing_employee_section`, `missing_fields`, `unexpected_fields`,
  `invalid_employee_id`, `id_normalization_changed`,
  `directory_mismatch`, `invalid_full_name`,
  `name_normalization_changed`, `invalid_is_active`,
  `invalid_created_at`, `duplicate_employee_id`. The employee ID is read
  from **inside** the manifest and only *checked* against the writer's
  own encoding (`safe_dir_name`); ID and name normalization are taken
  from the `Employee` model itself (never re-implemented) and a value
  the model would change is **rejected, never normalized**. `is_active`
  must be a real bool; `created_at` must be tz-aware ISO-8601 and is
  emitted **verbatim** (never replaced with export time). Duplicate IDs
  are ambiguous identity: every manifest in the group is rejected, so
  no record wins silently.
* **Artifact contract:** exactly five keys per line
  (`schema_version` 1, `employee_id`, `full_name`, `is_active`,
  `created_at`), UTF-8, LF, one trailing newline per record, ordered by
  `employee_id` code point — a stable export order, **not** a claim
  about PostgreSQL collation. No paths, sample data, image/embedding
  data, or biometric content ever appears.
* **Publication safety:** the artifact is written to a temporary file in
  the destination directory, flushed and fsynced, permission-restricted
  to owner-only where the platform supports it (reported honestly when
  it does not), then moved into place with `os.replace`; a failure
  removes the temporary file and leaves any previous artifact intact.
  An existing artifact is **never** replaced silently —
  `ExportOutputExistsError` (CLI exit 2) unless `overwrite=True`.
* **Fail-closed default:** any rejected manifest means **no artifact at
  all**. `allow_partial=True` (CLI `--allow-partial`) is the explicit
  opt-in that writes only the valid records and still reports every
  rejection. If entries exist but none validate, no artifact is written
  (an empty file could be mistaken for "no employees"); a genuinely
  empty manifest root does write an empty artifact, which is the honest
  representation of "no enrollments yet".
* **`src/config.py`:** new derived `AppSettings.exports_dir`
  (`<DATA_DIR>/exports`). Deliberately **not** added to
  `ensure_directories`: the directory holds names and is created only by
  an explicit export. `.gitignore` now ignores `data/exports/**`.
* **`tools/export_employee_metadata.py` (new):** operator CLI over the
  same code path (`0` success, `1` fail-closed/partial/write failure,
  `2` existing-artifact refusal). Its report contains counts, the
  artifact path, directory names, and reason codes only — **never an
  employee name**.
* **Testing (offline only):** new `tests/test_employee_export.py` →
  **94 passed, 4 skipped**. Coverage: exact five-key schema and line
  format, verbatim timestamps (`+00:00` and `Z`), non-ASCII names,
  leading zeros and a percent-encoded directory, deterministic ordering,
  every rejection code, missing/extra fields, unsupported version,
  malformed/non-UTF-8 JSON, directory mismatch, duplicate-ID grouping,
  symlink refusal (real symlinks where the platform allows it, plus the
  refusal branch simulated on Windows), output protection/overwrite,
  atomic-write failure leaving no partial artifact and preserving the
  previous one, owner-only permissions (POSIX), fail-closed and
  `--allow-partial` behavior, empty-root semantics, source-manifest
  immutability, sample/embedding-tree non-touch, and the CLI's exit
  codes and name-free output. `tests/test_config.py` gained the
  `exports_dir` assertion. The 4 skips are real-symlink creation (×3)
  and POSIX permission bits (×1), both unsupported on this Windows host.
* **Verification:** `pytest tests/test_employee_export.py -q` → **94
  passed, 4 skipped**; `tests/test_employee_export.py` +
  `tests/test_config.py` → 154 passed, 4 skipped; full offline sweep
  (camera / enrollment-CLI / YUNET-download suites excluded) → **1382
  passed, 0 failed, exit 0** — the same sweep with the new file excluded
  is **1288 passed**, so the delta is exactly this step's 94 passing
  tests. (Step 31's 1147 figure predates the owner's `60289ff` commit;
  1288 is the measured baseline at that commit.) `git diff --check`
  clean. Nothing staged or committed.
* **Deliberately NOT in this step:** the importer itself (Step 1.5c-2),
  any PostgreSQL/DSN access or real import (1.5c-3, which additionally
  needs the owner-provisioned dedicated test database, its marker, and
  the still-missing psycopg-backed runtime wiring), API endpoints, and
  any recognition change. Never-enrolled and deleted employees are
  outside this population, and recognition keeps gating on the
  manifest's `is_active` until a later approved step makes the database
  the read source — a documented, temporary divergence.

## Step 31 — Phase 3 Step 1.5b (H-3): session-scoped migration lock — Option B (owner-approved) (2026-11-03)

**Owner decision recorded:** OQ-1's *mechanism* is superseded — the
transaction-scoped `pg_try_advisory_xact_lock` first approved in Step
1.5a is replaced by a **session-scoped, nonblocking
`pg_try_advisory_lock` held on a dedicated connection for the entire
migration run**. The approval covers **only the lock mechanism**: the
approved per-migration transaction and ledger semantics (OQ-2/OQ-4) are
unchanged, and Option A (one outer transaction) was rejected for exactly
that reason. Implemented and offline-verified; **no PostgreSQL
connection was made**, and no runtime executor exists yet.

* **`src/db_migrate.py`:** the lock is now session-scoped and is the
  *first* executor call of a run — before ledger creation, table
  inspection, applied-version lookup and gap validation. It stays held
  across every per-migration COMMIT (that is the whole point of
  session scope), so a second runner cannot pass the state checks while
  the first is mid-run. Acquisition remains fail-fast and typed
  (`MigrationContentionError`), with no inference, waiting, sleep or
  retry.
* **Executor contract (ownership split):** the *executor* owns one
  dedicated lock connection; the *runner* owns its lifecycle. New
  abstract `release_advisory_lock()` must release the key, close that
  connection, and be **idempotent and no-op safe** (nothing acquired,
  not held, already closed → silent no-op; a second call never
  double-releases or touches a closed connection). The dedicated
  connection must stay open for the whole run — closing it silently
  drops the session lock — and the runner still performs no other
  connection management, retries, or re-acquisition.
* **Cleanup semantics:** one `finally` path covers success, typed
  failures, unexpected exceptions **and a failed acquisition** (the
  connection may already be open when acquisition blows up). A release
  failure never masks the original error: it is attached as a note when
  something else is propagating (`__notes__`), and becomes a typed
  `MigrationCleanupError` (new) when nothing else failed — the run's
  outcome is then explicitly unknown rather than silently assumed fine.
  A dropped lock connection means PostgreSQL released the lock and the
  run aborts: no recovery attempt.
* **Unchanged by this decision:** per-migration transactions with one
  ledger row each; a later failure never rolls back migrations that
  already committed; checksum re-verification, gap/missing-script/
  ledger-integrity refusals, the prohibited-statement scan and the
  `ADVISORY_LOCK_KEY` constant (still fixed, still not configurable).
* **Testing (offline only):** `tests/test_db_migrate.py` 40 → **53**
  (+13). New coverage proves, against a shared fake *server* (not a
  flag): acquisition precedes every state check; a second simulated
  runner is refused before it reads or writes state and its cleanup
  does not drop the first runner's lock; the lock is observed as held
  during each state check and each application; commits stay
  per-migration (`[1, 2, 3]`) with earlier commits surviving a failure
  at V2; release + connection close happen on success, typed refusal,
  unexpected exception and failed acquisition; a release failure
  becomes a note and never the reported cause; a release failure with
  no other error surfaces as `MigrationCleanupError`; and release is
  idempotent/after-close safe. Two obsolete assertions were *replaced*
  (the exact-call list now includes the mandatory `release`, and the
  contention test now asserts `acquire`+`release` only) — no assertion
  was weakened, and `naive now` was strengthened to prove the executor
  is never touched.
* **Verification:** `pytest tests/test_db_migrate.py -q` → **53
  passed**; `tests/test_employee_repository.py` → **25 passed**;
  `tests/test_test_database_guard.py` → **48 passed**; full offline
  sweep (camera / enrollment-CLI / YUNET-download suites excluded:
  hardware, real artifact, network) → **1147 passed, 0 failed, exit 0**;
  `git diff --check` clean. Nothing staged or committed.
* **Still unverified (unchanged):** the psycopg-backed executor, the
  no-serve-during-migration wiring, and any live-server behaviour of
  `pg_try_advisory_lock` including session-loss cleanup — no Tier-2
  database work is authorized or performed here.

## Step 30 — Phase 3 Step 1.5b: employee-metadata persistence foundation (offline, owner-approved OQ-1..7) (2026-11-02)

**Implemented offline on top of `bb95559` on `main`; no PostgreSQL
connection, no migration execution, no DDL against a real database, no
dependency change, and no camera/biometric/real-runtime-data access.**
The owner approved all seven OQ recommendations; this entry records
what was actually built and tested, never what remains.

* **`src/db_migrate.py` (new):** migration-runner core — deterministic
  `V<N>__<name>.sql` discovery (unparseable names/duplicates/version < 1
  refused), sha256 per-file checksums, fixed advisory-lock key
  acquired via the executor **before any state check** (typed
  fail-fast `MigrationContentionError`; no inference, no waiting),
  per-migration ledger rows with checksums (OQ-2 approved shape:
  `schema_version(version, checksum, applied_at)`, runner-materialized
  — migrations never create the ledger), checksum re-verification of
  already-applied scripts (modified-applied → typed refusal),
  missing-script/non-contiguous-ledger/employee-without-ledger
  integrity refusals, strict version-N-only-at-N−1 pending window, per
  migration one executor-managed transaction (failure aborts the run;
  later versions never attempted; typed sanitized
  `MigrationApplicationError` with the cause type name only),
  prohibited-statement scan (`CREATE DATABASE` /
  `CREATE INDEX CONCURRENTLY`) over comment-stripped text, and the
  fully sanitized typed error family (no file paths, no SQL content,
  no driver text; causes preserved for trusted server-side use only).
* **`migrations/V001__employee.sql` (new):** the approved
  `employee` table verbatim — four metadata columns, `created_at`
  with **no database default** (application-authored), no
  biometric/embedding/attendance-event tables.
* **`src/employee_repository.py` (new):** create (explicit Typed
  duplicate via the driver's SQLSTATE 23505, never a raw driver error —
  `sqlstate` after the H-1 audit repair), get
  (`None` → caller's 404), list (**keyset-only** `employee_id > after`,
  default 20 / hard clamp above 100 per OQ-6 as frozen
  `PaginationConfig` in `src/config.py`; values above the max are
  clamped, sub-1/non-int refused before any DB call; **no
  cross-request snapshot claim**), `update_full_name` (validated
  through the existing model first; SQL touches `full_name` only;
  `is_active`/`created_at`/`employee_id` untouched; returns the
  read-back row), deactivate (persisting status only; written back as
  read-back row). Availability/config errors propagate untouched
  (`DatabaseUnavailableError`, `MalformedDsnError`); duplicate
  detection is typed `EmployeeAlreadyExists`; missing employee →
  `EmployeeNotFound`. No connection at import or construction time.
* **`src/config.py`:** new frozen `PaginationConfig` registered as
  `PAGINATION` (default 20, max 100, validation mirrors the existing
  constants pattern). Attendance behavior untouched; no attendance
  pagination exists to change.
* **`src/test_database_guard.py` (new):** Tier-2 positive-verification
  guard per approved OQ-5 — marker `_phase3_test_guard` with fixed
  phase-tagged literal `VISUAL_ATTENDANCE_PHASE3_TEST_ONLY`; **both**
  DSNs required before any verification; normalized identity
  comparison (dbname/host-normalized/port; text-alias and
  credential differences can neither fake nor enable distinctness);
  connection identity read from the connection object
  (`connection.info` then `current_database()`, never reconstructed);
  read-only SELECT discipline until every ordered check passes; the
  marker's `schema_version` cross-checked against the applied ledger;
  destructive-action manifest gate (`require_destructive_action`);
  every missing/ambiguous/inconsistent condition → typed
  `GuardBlocked` **fail closed**, never a name-pattern guess, never a
  fallback to the application database. The DSN parser is an
  injectable hook so offline tests run psycopg-free in-process (the
  API suite's "psycopg unloaded" tripwire stays honest under any
  ordering); the default `src.db` parser is proven in fresh
  subprocesses instead.
* **Testing (all offline, synthetic):** `tests/test_db_migrate.py` 53,
  `tests/test_employee_repository.py` 25,
  `tests/test_test_database_guard.py` 48 — 126 total (the initial 1.5b
  gate had 26 + 20 + 24 = 70; the audit repairs took it to 113; the H-3
  Option B lifecycle tests in Step 31 took it to 126). Includes
  fresh-process default-parser and import-hygiene proofs. Regression: full offline suite
  sweep `pytest tests/` minus the hardware/external-file suites
  (camera, enrollment CLI, YUNET download) → **1147 passed, 0
  failed**; the focused new+existing Phase 3 order
  (api/config/db/environment/api_guard + new three) also green.
  One test-isolation fix during the run: the guard suite initially
  leaked psycopg into the shared pytest process via the default DSN
  parser; resolved by the injectable-parser hook (no existing test
  weakened).
* **Independent-audit repairs (read-only audit of 1.5b; all fixed
  offline, no database contacted):**
  * *H-1* — duplicate detection moved from psycopg2's `pgcode` to
    psycopg 3's `sqlstate == "23505"`. The old attribute does not exist
    on the pinned driver (`psycopg[binary]==3.3.6`, verified directly:
    `Error.pgcode` is absent, `Error.sqlstate` exists), so the typed
    `EmployeeAlreadyExists`/409 path could never fire; the fake driver
    had manufactured `pgcode`, which is why 20/20 offline tests passed.
    The fake now models psycopg 3, and unrelated SQLSTATEs (`23502`),
    a psycopg2-only `pgcode`, and exceptions with no SQLSTATE are
    proven to propagate untouched.
  * *H-2* — the prohibited-statement scanner missed
    `CREATE UNIQUE INDEX CONCURRENTLY` (and comment-split forms); it now
    matches `CREATE [UNIQUE] INDEX CONCURRENTLY` and
    `CREATE DATABASE` on comment-stripped, whitespace-collapsed text.
    Reported honestly as a scanner, not a SQL parser: `/* */` is not
    nested and string literals are not special-cased, so both
    limitations can only cause an over-refusal.
  * *M-1* — identity comparison fails closed: `hostaddr`, `service`, a
    missing host, or a missing database name → `IDENTITY_AMBIGUOUS`
    instead of being treated as proof of distinctness; an omitted port
    is libpq's 5432; the driver-reported connected host/port must match
    the declared target (`CONNECTED_TARGET_MISMATCH`) and differ from
    the application target.
  * *M-2* — read-only is now **enforced** (`connection.read_only = True`,
    read back before the first query); failure → `READ_ONLY_UNAVAILABLE`
    with nothing read.
  * *M-3* — the verification connection is closed on success and on
    every blocked path (`try`/`finally`), without committing.
  * *M-4* — migration checksums hash canonical text (BOM dropped,
    CRLF/CR → LF), so the repository's `core.autocrlf=true` checkout
    cannot fake a modified migration, while any SQL edit still changes
    the digest.
  * *M-5* — `VerifiedTestTarget` can no longer be constructed directly
    (private proof), manifests must be collections of non-blank strings
    (`MANIFEST_INVALID`), and the module states plainly that this is a
    mistake-guard library contract, **not** a security boundary against
    in-process code.
  * *L-1..L-5* — corrected the DB_DESIGN test count (71 → 70 at the
    1.5b gate), replaced "nothing in this section is implemented" with
    per-section implementation status, split a misnamed migration test
    into a real file-validation test plus a post-lock gap test, removed
    a tautological pagination assertion, distinguished marker-absent /
    read-failure / ambiguous rows, and fixed the CHANGELOG
    "unlicensed" wording.
* **OQ-1 mechanism reopened by the audit, then RESOLVED by the owner —
  Option B, implemented in Step 31.** The audit found the approved
  transaction-scoped `pg_try_advisory_xact_lock` incompatible with one
  transaction per migration: taken before the state checks, a
  transaction-scoped lock is released before the migrations it
  protects, and holding that transaction open instead would merge the
  whole run into one commit. The two options considered were:
  * **A — one outer transaction** (lock + checks + DDL + ledger share
    it). Pro: the lock is genuinely held for the run and the code stays
    simple. Con: a later migration's failure rolls back *all* migrations
    in that run, and per-migration commit granularity is lost —
    contradicting the OQ-2/OQ-4 per-migration ledger design.
  * **B — session-scoped `pg_try_advisory_lock` on a dedicated
    connection** held for the whole run, each migration in its own
    transaction, released in `finally` and the connection closed.
    Pro: keeps per-migration atomicity and the approved fail-fast
    policy; a crashed runner still leaks nothing (the OS closes the
    session). Con: changes the approved lock *mechanism* (needs owner
    approval), requires an explicit release path, and ties the lock to
    connection lifetime, so the executor contract must expose that
    lifetime.
  * **Recommendation: Option B** — it is the only one that preserves
    the already-approved per-migration ledger/commit semantics; Option
    A silently trades that away.
  The owner **approved Option B**, and it is implemented and
  offline-tested in Step 31. This block is kept as the decision
  history; nothing here is still pending.
* **Deliberately NOT in this step (unverified, later gates):** the
  real psycopg-backed `MigrationExecutor`, the no-serve-during-
  migration runtime wiring, any Tier-2 integration test against a
  live database (requires owner-provisioned dedicated test database,
  the established marker, and `ATTENDANCE_TEST_DATABASE_URL` — Step
  1.5b-3), the JSONL→PostgreSQL employee importer (Step 1.5c, source
  format still an open decision), and all API endpoint
  implementations (all seven non-health routes remain 501).

**Verification:** baseline `bb95559` clean + synced; 3 pre-existing
Step 1.5a doc changes preserved untouched; nothing staged or committed.

## Step 29 — Phase 3 Step 1.5a: DB design planning record for employee metadata (planning-only; nothing implemented) (2026-10-31)

**Planning gate, no code and no database activity.** Wrote
[DB_DESIGN.md](DB_DESIGN.md) and corrected stale Step 27/28 commit-status
wording; reworded the `.env.example` test-DSN block. The record fields:

* **Provenance split:** conversation-derived decisions recorded *as
  supplied* (employee table contract, rollback strategy, test-guard
  contract, repository/API rules) vs. statements verified directly in
  the repository at `bb95559` (marked [REPO] with per-item evidence).
* **`employee` table contract recorded here** (owner-approved in the
  decision record, not by this planning gate): exactly the four
  metadata columns (`employee_id` PK, `full_name`, `is_active`,
  `created_at TIMESTAMPTZ NOT NULL`, **no DB default** —
  application-authored via `datetime.now(UTC)`); no biometric/
  embedding/attendance-event tables.
* **Migration contract drafted:** forward-only `migrations/` SQL,
  per-migration transaction with an atomic `schema_version` ledger row,
  gap-free version checks, sanitized failure reports (no DSN/driver
  text), full-backup-restore rollback (no down-migrations), destructive
  ops prohibited outside a positively verified disposable test target.
* **Repository contract drafted:** create/get/list(PAGE 20, max 100,
  keyed-offset ordering)/update_full_name/deactivate; 409/404/422/503
  mappings, `EmployeeAlreadyExists`/`EmployeeNotFound` typed errors,
  blank-DSN and unavailable paths reusing `src.db`'s existing taxonomy.
* **Tier-2 test safety contract drafted:** five ordered positive checks
  (+ typed BLOCKED) before any destructive setup, including the
  `phase3_test_guard` marker as a *server-defined truth*, never
  inferred from names.
* **Open decisions flagged for owner review before 1.5b:** runner
  concurrency policy, ledger shape, rollback medium, migration storage
  form, marker details, pagination defaults, JSONL importer timing.

**Verification:** baseline `bb95559` clean and synced; documentation
only — no code/test/dependency change, no PostgreSQL connection, no
migration, no camera or real data access.

## Step 28 — Phase 3 Windows single-process startup guard: OS-enforced msvcrt byte-range lock, guarded `run_server` lifecycle, typed exit-code-3 runner handling (Step 1.4) (2026-10-30)

**Step 1.4, implemented on top of the Step 1.3 foundation (committed by
the owner as `bb95559`):**

* **`src/api_guard.py` (new):** Windows-only startup guard per the
  finalized Phase 0 contract. Lock file `DATA_DIR/api_guard.lock` — the
  path is derived from the validated `AppSettings.data_dir`, never
  re-parsed. Opened `a+b` (never truncates); the initial byte is
  written only when the file is empty, so repeated startups (and a
  guard file left by an abnormal termination) never grow it.
  Acquisition locks **one byte at offset 0** with a nonblocking
  exclusive `msvcrt.locking(..., LK_NBLCK, 1)` after `seek(0)`; the
  handle stays open for the guarded server's whole lifetime. Contention
  and acquisition/creation failures raise the typed
  `StartupGuardError`; non-Windows platforms raise
  `StartupGuardUnavailableError` (deterministic, no POSIX fallback —
  a Python-level flag cannot guarantee exclusivity, so non-win32 must
  not *appear* guarded). Exception messages are fixed sanitized
  constants — no paths, environment values, or raw OS error text
  (trusted detail stays with `__cause__`/server-side logging).
* **`src/api.py` (minimal, disclosed Step 1.4 scope adjustment — the
  file owns `run_server`; flag it for owner confirmation):**
  `run_server` acquires the guard **before** any request can be served
  and releases it via the context manager on orderly shutdown; Uvicorn
  is imported only after successful acquisition. `--print-bind` and
  app construction never acquire the guard. The eight approved
  `/api/v1` routes, loopback binding, and error envelopes are untouched.
* **`tools/run_api.py`:** `run_server` guard failures are translated to
  the documented **exit code 3** (distinct from the configuration exit
  code 2) with a single sanitized line — no traceback, no paths.
  `--print-bind` remains guard-free (it does not serve).
* **Windows facts verified empirically (probes + tests, win32/Python
  3.14, temp directories only):** contention from an independent process
  is denied (`OSError` 13 on `LK_NBLCK`); a **byte read of the locked
  range is likewise denied** (`PermissionError`) — the lock byte can be
  verified only after release; orderly release and abrupt termination
  (`os._exit(1)`, `TerminateProcess`) both leave the file unlockable
  immediately (the OS releases the byte range with the process); a
  stale file never blocks acquisition and is never rewritten.
* **`tests/test_api_guard.py` (new):** 14 tests — in-process
  acquire/release/idempotent-byte/stale-file/context-manager/sanitization
  contracts; real subprocess acquisition vs contention; acquisition
  after orderly release (`RELEASED`) and after crash release
  (`os._exit(1)`); stale file does not block; failed acquisition stops
  before any Uvicorn import (fresh-process proof: `uvicorn` never in
  `sys.modules`); the guarded runner serves healthy on an ephemeral
  loopback port, the lock stays held while serving, and abrupt
  termination reopens acquisition; an **orderly real-Uvicorn shutdown
  (in-process recorded `uvicorn.Server`, graceful `should_exit`)
  releases the lock** — subprocess `CTRL_BREAK_EVENT` was rejected as
  the orderly path because uvicorn does not translate `SIGBREAK` into a
  graceful stop; the runner exits 3 with sanitized stderr and nothing
  listening under contention; `--print-bind` never acquires.
  Every child is killed and reaped in `finally` blocks; all fixtures
  use per-test temporary `DATA_DIR` values (the project's real
  `data/` directory is never touched). Non-win32 platforms skip the
  file (documented, deliberate).

**Verification observed (2026-10-30 session, hardware-free, synthetic
data only, Windows x64):** guard suite **14 passed**; config/db/
environment/API foundation **120 passed**; core recovery/engine/store/
persistence/employee battery **265 passed**; integration/durable/report
suites **153 passed**; live-runner subprocess evidence captured inside
the tests (health 200 while lock held; contention exit 3; crash and
orderly release then acquisition). `git diff --check` exit 0. No
PostgreSQL connection, no migration/schema change, no journal access,
no camera, no real biometric/employee data, no dependency change, no
Git mutation.

## Step 27 — Phase 3 API foundation: approved eight-endpoint application, settings-driven loopback runner, sanitized error surface (Step 1.3) (2026-10-30)

*(Phase 3 relatedly completed earlier, without its own status entry: Gate
1.1 dependency verification and Gate 1.2 configuration/typed-DB layer
(`requirements.txt` pins `fastapi==0.162.0` / `uvicorn` / `psycopg[binary]==3.3.6`,
`src/config.py` `ATTENDANCE_DATABASE_URL`/`ATTENDANCE_TEST_DATABASE_URL`/
`API_HOST`/`API_PORT` with strict loopback-only binding, `src/db.py` lazy
connection layer with DSN-redaction hardening, `.env.example`). Verify
against commit `4a944d4`.)*

**Step 1.3, implemented on top of the Gate 1.2 baseline (committed by
the owner as `601341b`):**

* **`src/api.py`:** FastAPI application foundation. The served inventory
  is **exactly** the approved eight `/api/v1` endpoints
  (`GET /health`; the seven non-health approved routes registered at
  their approved methods, each answering a typed `501 NOT_IMPLEMENTED`
  envelope closing no business decision, reaching no database, and
  claiming no attendance operation — deferred to authorized later
  steps). Docs URLs (`/docs`, `/redoc`, `/openapi.json`) are disabled so
  the inventory is exactly eight. No CORS/auth middleware is installed.
  Only `GET /api/v1/health` is implemented (`{"status": "ok"}`; no
  paths/credentials/employee data/DB details). Error responses are one
  stable envelope — `"error": {"code", "message"}` — with machine
  codes separate from human messages: `HTTP_ERROR` (registered against
  **Starlette's base `HTTPException`** so framework-generated
  404/405 route errors go through the same sanitized envelope — covered
  by live-verified tests), `REQUEST_INVALID`, `INTERNAL_ERROR` (last-
  resort 500: logs trusted diagnostics server-side via
  `src.logging_setup`, the client sees only a generic constant), plus
  `CONFIG_INVALID` / `DATABASE_UNAVAILABLE` / `DATABASE_CONFIG_INVALID`
  handlers reserved for the database-backed step (downstream of the
  Gate 1.2 redaction machinery, keeping malformed-DSN and unavailable
  classifications distinct). Importing the module and building the app
  connect to no PostgreSQL and import no driver, CV stack,
  attendance/camera/enrollment module, or the `uvicorn` server runtime
  (fresh-subprocess enforced by test).
* **`tools/run_api.py`:** settings-driven loopback server entry point.
  Bind host/port are the already-validated `AppSettings.api_host`/
  `api_port` values — never duplicated or bypassed. Non-loopback or
  invalid `API_HOST`/`API_PORT` fails fast: exit code 2, one sanitized
  line, **no traceback and no server start** (the invalid-configuration
  classification happens at the settings *import* boundary and the
  failed `src.config` module is deliberately never re-imported).
  `--print-bind` prints the validated bind and exits without serving.
  Single-process; a startup lock guard follows its finalized design
  contract in a later authorized step.
* **`tests/test_api.py`:** import-hygiene subprocess test (banned-module
  list), exact-eight route inventory, no-middleware assertion, real
  ephemeral-loopback uvicorn servers driven by stdlib HTTP (server
  fixture + forbid-DB tripwire asserting `src.db._connect` is never
  called and psycopg stays unloaded), live health + deferred-endpoint
  501 envelopes, unknown-route 404 and unsupported-method 405 in the
  stable envelope, credential-bearing synthetic-exception 500
  sanitization (no DSN/password/traceback/path leakage), the four typed
  code handlers asserted as unit contracts, and the runner CLI
  (loopback bind print, defaults, invalid-host refusal with exit 2).

**Verification observed (2026-10-30 session, hardware-free, synthetic
data only):** `tests/test_api.py` **24 passed**; config/db/environment
regression **96 passed**; core recovery/engine/store/persistence/employee
battery **265 passed**; integration/durable/report/CLI suites **153
passed**; `git diff --check` exit 0. Live subprocess smoke via the real
runner + curl: `/api/v1/health` 200, unknown route 404 envelope,
unsupported-method 405 envelope, all seven deferred routes 501
envelopes, `API_HOST=0.0.0.0 --print-bind` exit 2 with sanitized stderr,
blank stdout/server-sharing-free log; smoke server stopped by its
recorded PID. No PostgreSQL connection, no migration/schema change, no
journal access, no camera, no real biometric/employee data, no
dependency change, no Git mutation.

## Step 26 — check-out event support: independent bounded engine state, journal-backed session recovery, single-snapshot startup, checkout-aware reporting (Phase 2, P2-6) (2026-10-27)

**P2-6, implemented in owner-authorized stages S1–S6** on `main`:

* **S1 (`src/config.py` + tests):** additive
  `AttendanceConfig.check_out_cooldown_seconds = 300.0`, validated like
  the existing cooldowns (booleans/non-finite/zero/negative rejected);
  `check_in_cooldown_seconds = 300.0` untouched. The default is an
  engineering value, not an organization-calibrated policy.
* **S2+S2.1 (`src/attendance.py` + engine tests):** `CHECK_OUT` is an
  `EventType` with its own serialized value `check_out` (envelope and
  schema version unchanged); the engine keeps **three independently
  bounded maps** — check-in cooldown anchors, check-out cooldown
  anchors, open-session markers — each at `max_tracked_employees`,
  eviction in one never touching the others. Open-session decisions:
  accepted `CHECK_IN` opens/refreshes the marker (last writer wins);
  checkout without a known open session is rejected with no event and
  no state change; an accepted `CHECK_OUT` closes the session and
  starts the independent check-out cooldown without touching the
  check-in anchor; cooldown-rejected checkouts preserve the session.
  An optional `open_session_lookup` hook adjudicates in-memory misses
  via a fresh external query: strict Boolean contract, typed error on
  non-Boolean, propagated exceptions, never silently `False`. No
  automatic checkout trigger — a checkout only occurs via an explicit
  decision request.
* **S3 (`src/attendance_recovery.py` + tests):** one pure
  `fold_latest_events(events)` — per employee, the max of
  `(occurred_at, journal_position)` (later timestamp wins, equal
  timestamps by later journal position; UTC-normalized datetime
  comparison; deterministic per-employee isolation) — shared by
  recovery and the new `make_journal_open_session_lookup(store)`
  lookup factory (fresh validated `load_events()` per query, errors
  propagated). `recover_session_state` restores an open-session marker
  for a latest-`CHECK_IN` employee and a check-out cooldown anchor
  only when the latest event is `CHECK_OUT`; a corrupt journal fails
  closed with no partial restoration. Additive engine APIs
  `restore_open_session` / `restore_check_out_anchor` mirror the P2-3
  restoration pattern (wall-to-wall ages; expiry decided by the
  engine's configured cooldown).
* **S4 (`src/integration_demo.py` + durable tests):** both durable
  paths run single-snapshot startup recovery
  (`recover_attendance_startup_state`: **one** validated journal load
  feeding the P2-3 check-in-anchor phase and the P2-6
  session/checkout-anchor phase) **before** any attendance processing,
  detector load, or camera open; corrupt or unreadable journals fail
  closed before any hardware is touched. The durable engine's open-
session hook is the journal-backed lookup (fresh load per query,
  never cached). Non-durable behavior and defaults are unchanged;
  durable mode remains opt-in behind `--durable`; the journal stays
  check-in-only under ordinary attendance (no invented trigger).
* **S5 (`tools/attendance_report.py` + tests):** the report summary no
  longer counts check-outs as check-ins. The `CHECK_INS` table counts
  `check_in` events only (byte-identical for pre-P2-6 journals); an
  additive `CHECK_OUTS` table counts `check_out` events only when the
  selection contains at least one checkout. The journal is never
  rewritten; exit codes and flags unchanged.
* **S6 (docs only):** README compatibility/upgrade section, this
  status entry, ARCHITECTURE D23, changelog entry.

**Verification observed (2026-10-27 session, hardware-free, synthetic
data any):** recovery suite 56 passed; core battery (recovery + engine +
config + store + persistence) 258 passed; integration/durable/report
suites 104 passed; integration-demo CLI suite 49 passed; report CLI
suite 40 passed (36 prior + 4 S5-focused); `git diff --check` exit 0.
No camera, no real biometric/attendance data, no Git mutations.

## Step 25 — opt-in durable composition wired into live and smoke modes (Phase 2, P2-5) (2026-10-24)

**Owner-approved wiring milestone only: P2-1 through P2-4 components
were real, tested, and deliberately unwired; Step 25 wires them into
OPT-IN paths (`--durable` shared by `--smoke` and `--live`, default
OFF) and touches no component contract. Default-off behavior and
output are byte-identical to the pre-P2-5 ones (probe-tested: nothing
constructed, nothing loaded, nothing written). Full suite 994 passed
(965 prior + 29 new = 13 CLI + 16 durable-mode), focused durable suites
65 passed, relevant suites 260 passed (exit 0 each, `pipefail`
captured); camera never opened during automated work; no real
biometric/enrollment/journal data accessed; nothing staged or
committed.**

**Implemented**

* `tools/run_integration_demo.py` — one `--durable` flag (not
  mode-specific, default off; parsing/help do not touch hardware);
  smoke-mode propagation plus a clearly labeled durable summary
  (recovered anchors, the four persistence-outcome counts, journal
  location note); live-mode propagation via `LiveModeConfig(durable=…)`
  with the other live options provably unaffected; a durable smoke run
  halted by a journal-load/recovery error exits `1` with the typed
  error surfaced — no traceback, no silent degradation.
* `src/integration_demo.py` — `run_smoke(…, durable=False,
  wall_clock=None)`: durable runs compose the real engine with the
  P2-1 store through the unchanged P2-2 service, and the P2-3 replay
  runs **before any frame is processed**; the exact four outcome
  states are reported verbatim (never redefined); engine decisions,
  statuses, thresholds, and the no-event-honesty path are unchanged;
  `SmokeRunResult` carries additive default fields
  (`durable_outcomes`, `durable_recovered_anchors`). Default runs keep
  the exact pre-P2-5 engine construction (wall clock untouched).
* `src/integration_demo.py` — `run_live`: durable composition and
  recovery happen **before** the detector load and camera open (the
  ordering is enforced by construction and probe-tested with recording
  doubles: a corrupt journal raises before any hardware load); a
  journal-load or recovery error fails closed — the run stops before
  processing frames in smoke and before anything opens in live;
  verified frames print the unchanged `VERIFIED …` line plus one
  `durable:` line for PERSISTED / REJECTED / UNCONFIRMED outcomes
  (`NO_EVENT` silent); uncertain appends are never retried;
  unknown-face handling, recognition, temporal policy, and cleanup are
  exactly as before; the flag-off path never constructs a store.
* `tests/test_durable_live_mode.py` (new, 16 tests) + 13 new CLI tests:
  parsing/default-off/propagation; happy-path persistence (exact
  outcome sequence, timestamp agreement, five-key journal privacy
  envelope); restart simulation (journal replayed into a fresh run →
  suppression restored, nothing re-appended); cooldown boundary (299 s
  suppressed; exact 300 s expired → fresh event); rejected/unconfirmed
  failures surfaced with exactly one append attempt and provably
  unchanged/empty journals; corrupt journal and unsupported schema
  failing closed; live durable ordering and flag-off journal purity
  under injected camera/detector doubles.

**Explicit limitations (never overclaimed)**

* Real-camera durable behavior is NOT verified — no owner `--live
  --durable` session has been recorded; automated verification was
  hardware-free (injected doubles only).
* Durable smoke journals inside the run's temporary directory (removed
  with it); durable live journals to the configured
  `data/attendance/events.jsonl` (UTC only, unchanged schema).
* Empirical CV accuracy evaluation remains BLOCKED pending the required
  consented labeled dataset and a real `models/sface.onnx` evaluation
  run (Step 14) — untouched by this milestone.
* Explicit non-goals held: no check-out/shift/lateness/daily-dedup
  business rules (separate milestone, sequence unchanged); no
  thresholds lowered or auto-tuned; no unknown-face biometrics stored;
  no frames, crops, embeddings, or similarity scores logged; the
  report CLI (P2-4) remains an on-demand operator tool, not part of
  any run loop.

## Step 24 — read-only attendance report CLI (Phase 2, P2-4) (2026-10-24)

**Read-only operator inspection only — the live demo is still NOT wired
to persistence or to this CLI; no journal schema, store behavior,
recovery API, threshold, or `src/` file changed; the P2-2 four-state
outcome contract is unchanged; stdlib only (no new dependencies).
Focused CLI suite 36 passed, full suite 965 passed (929 prior + 36
new), `git diff --check` clean (exit 0 each, captured under
`pipefail`); no camera opened, no real biometric/enrollment/attendance
data accessed; nothing staged or committed.**

**Implemented**

* `tools/attendance_report.py` — read-only listing/filter/summary over
  the P2-1 journal via the unchanged `load_events()`. Deterministic
  chronological default order (`occurred_at`, ties by journal
  position) with `--journal-order` alternative; verbatim repeatable
  `--employee`; UTC half-open date filtering (`--since`/`--until`/
  `--day`, fractional seconds covered by the next-midnight exclusive
  bounds); `--summary` per-employee × per-day counts. Duplicate
  `event_id` records display as-is (never de-duplicated, never warned
  about). Nothing is ever appended, truncated, repaired, rewritten, or
  locked; journal bytes are asserted byte-identical after every
  journal-touching test run.
* Exit contract (argparse default exit overridden so bad arguments are
  `3`, `--help` untouched at 0): `0` success — including empty journal
  and torn trailing record (valid rows print, a stderr warning
  surfaces the loader's excluded-fragment flag, never repaired); `2`
  corrupt/unsupported/unreadable journal — typed store error on
  stderr, NO rows (a failed load is never presented as an empty
  report); `3` invalid arguments/dates, validated before any journal
  access.
* Import/execution hygiene (tested): importing the tool module loads
  no `src` modules (store imports deferred into `main()` — the store
  module graph transitively includes recognition-layer types);
  executing a report imports the store but never constructs a camera,
  detector, or recognizer model (constructor-failure guard test);
  importing creates no data-directory tree.

**Explicit limitations (never overclaimed)**

* Not wired into the live attendance workflow (no caller; the live
  demo remains unconnected to persistence).
* UTC-only display and filtering; site-local timezone support is
  deliberately deferred (the journal stores UTC only).
* Explicit non-goals held: no check-out/shift/daily-dedup business
  rules (separate milestone, sequence unchanged), no CSV/JSON export,
  no recovery invocation, no live-mode wiring, no journal repair.

## Step 23 — restart recovery of attendance cooldown anchors (Phase 2, P2-3) (2026-10-23)

**Read-only replay only — the live demo is still NOT wired to the
store or to recovery; no decision logic, journal schema, threshold,
or store contract changed; the P2-2 four-state outcome contract is
unchanged. Focused suites 96 passed (`pytest
tests/test_attendance_recovery.py tests/test_attendance_engine.py -q`;
28 recovery + 16 new engine tests plus those modules' existing tests),
relevant suites 212 passed, full
suite 929 passed (exit 0 each, captured under `pipefail`); no camera
opened, no biometric artifact accessed; nothing staged or committed.**

**Implemented**

* `src/attendance_recovery.py` — `recover_cooldown_anchors(engine,
  store, *, wall_now)`: loads the journal in full via the P2-1
  `load_events()` before any engine mutation (load failures propagate
  unchanged — never treated as empty), selects per employee the
  latest persisted `CHECK_IN` by UTC `occurred_at` (ties by journal
  position; deterministic under out-of-order records and duplicate
  `event_id`s), and restores unexpired anchors read-only. Nothing is
  appended; no event is created; torn-tail flag passed through on the
  frozen `CooldownRecoveryReport`.
* `src/attendance.py` (additive) —
  `AttendanceEngine.restore_suppression_anchor(employee_id, *,
  elapsed_seconds)`: anchor at `monotonic_now - elapsed_seconds` in
  the current run's monotonic domain; `False` when already expired;
  the engine's own cooldown, validation, and LRU bounded-state policy
  apply unchanged (wall timestamps are never treated as monotonic).
* Clock-skew policy (explicit, conservative): backward wall clock →
  negative age clamped to zero (full cooldown from recovery; worst
  case one delayed legitimate check-in, never a duplicate); forward
  wall clock → expired anchors not restored (worst case one extra
  check-in, the engine's documented bounded-state worst case).
  Honest limit recorded: cross-run cooldown precision is bounded by
  wall-clock reliability between runs; within a run, suppression is
  monotonic-exact.
* Tests: `tests/test_attendance_recovery.py` (28) +
  `TestRestoreSuppressionAnchor` in `tests/test_attendance_engine.py`
  (16) — empty journal, exact boundary, independence, latest-event
  selection, real-chain replay (suppressed frames never become
  events), expiry, out-of-order/duplicate determinism, torn tail,
  corrupt/unsupported-schema all-or-nothing, byte-identical journal
  after replay, clock-skew policy, replay-then-process equivalence.

**Not done (explicit)**

* No live-mode or CLI wiring (recovery has no caller); no check-out,
  daily dedup, multi-person tracking, unknown-face persistence,
  database, or API work; no writes or retries during replay.

## Step 22 — durable attendance composition (Phase 2, P2-2) (2026-10-23)

**Composition only — the live demo is still NOT wired to the store;
no decision logic, schema, threshold, or store contract changed.
Focused suite 19 passed, relevant suites 166 passed, full suite 883
passed (exit 0 each, captured under `pipefail`); no camera opened, no
enrollment/deletion run, no biometric artifact accessed; nothing
staged or committed.**

**Implemented**

* `src/attendance_persistence.py` — `DurableAttendanceService`
  composing the unchanged Step 13 `AttendanceEngine` (decision
  authority) with the unchanged P2-1 `AttendanceEventStore`
  (persistence/validation authority). `process(result)` passes the
  temporal result to the engine unmodified; when the decision carries
  no event **nothing is appended**; when it does, that exact event
  object is appended exactly once. Zero decision logic added; zero
  storage semantics added.
* Outcome contract — frozen `DurableAttendanceOutcome` with exactly
  four `DurableAttendanceStatus` states: `NO_EVENT` (decision carried
  verbatim; nothing appended), `PERSISTED` (append returned; fsync
  confirmed), `PERSISTENCE_REJECTED` (append raised
  `AttendanceStoreInputError` — a **confirmed** failure: by the
  store's documented contract its input rejections occur before any
  write, so the journal is provably unchanged and the record is
  definitively absent), and `PERSISTENCE_UNCONFIRMED` (any other
  `AttendanceStoreError` — an **uncertain** outcome: bytes may or may
  not have reached the journal; the **original exception object** is
  carried unmodified on `store_error` with cause chain intact, plus
  the exact event and a short reason naming the case). A persistence
  failure can never be presented as a durable check-in, and **no
  automatic retry** is attempted in either failure state — after an
  uncertain I/O failure the journal may already contain the record,
  so retries are the caller's decision.
* Failure classification (never mislabeled): the store's documented
  error family splits by its own semantics — pre-write input
  rejections are confirmed (`PERSISTENCE_REJECTED`), all other store
  errors are uncertain (`PERSISTENCE_UNCONFIRMED`); any other store
  exception propagates unchanged (a programming error is not a
  persistence outcome); engine `AttendanceInputError` propagates
  unchanged with nothing appended. Collaborators are validated
  structurally (callable `update`/`append` — the engine's injectable
  pattern), so injected doubles work; construction performs no
  filesystem work.
* Privacy: outcomes carry decisions, events (IDs/types/timestamps),
  statuses, and short reasons only; log lines carry statuses and
  exception class names — never employee IDs, never biometric data.
* `tests/test_attendance_persistence.py` — 21 deterministic,
  hardware-free tests: outcome matrix (no-event ×3 decision
  statuses with zero appends and verbatim decision carriage; persisted
  with exactly-once same-object append; confirmed rejection vs
  uncertain persistence with the distinct classifications and the
  original exception preserved by identity in both); no automatic
  retry across two failed processes (exactly one append attempt
  each); non-store `ValueError` propagates unlabeled; engine
  exception propagation with zero appends; frozen outcome dataclass;
  construction validation (doubles accepted, non-callable rejected)
  and no filesystem side effects; real-engine integration with
  injected fake clocks (VERIFIED → exactly one event with exact
  fields; same-instant repeat SUPPRESSED → no additional append; exact
  300 s cooldown boundary → second event); and a real-chain
  integration test — real `AttendanceEngine` + real
  `AttendanceEventStore` + temporary journal (no real `data/`):
  `PERSISTED` with exactly one journal record matching the
  engine-created event, and a suppressed repeat leaving the record
  count unchanged.
* Follow-up clarification (2026-10-23): the former single
  `PERSISTENCE_UNCONFIRMED` mapping for every store error was split
  into the confirmed `PERSISTENCE_REJECTED` (input rejections,
  pre-write) and the uncertain `PERSISTENCE_UNCONFIRMED` (all other
  store errors), so the public outcome now accurately distinguishes
  "failed, journal unchanged, safe to correct and retry" from
  "uncertain, reconcile by event_id before retrying". No decision,
  schema, or store-contract change; the fix-iteration itself caught a
  missing import before any pass was claimed.

**Known limitations / honest boundaries**

* Live mode, the smoke demo, and the CLI tools are untouched — the
  service has no caller yet; wiring it into `run_live` is the next
  step's explicit work, not done here.
* No retry/reconciliation policy is implemented (deliberately): after
  an uncertain append the caller must consult the journal (by
  `event_id`) before deciding to re-append; nothing in this service
  checks for it.
* Engine cooldown anchors remain engine-instance-lifetime; durable
  anchor recovery across restarts is future work (P2-3).

## Step 21 — durable attendance event store (Phase 2, P2-1) (2026-10-22)

**Post-audit error-contract fix (2026-10-22):** an event ID carrying a
lone Unicode surrogate passes frozen event construction but fails UTF-8
encoding at write time, previously escaping as an unwrapped
`UnicodeEncodeError`. `append()` now raises the documented typed
`AttendanceStoreInputError` (cause preserved; invalid input, not a
persistence failure — Python encodes before buffering, so verified
byte-identical journal, no partial data). New boundary tests: surrogate
ID rejection (both ID fields), zero-byte journal load, directory at the
journal path (load = empty history; append = typed persistence error,
pinned in both directions), and a *complete valid* trailing fragment
missing only its newline excluded-and-flagged on load. Focused suite 53
passed, full suite 864 passed (exit 0 each, captured under `pipefail`).

**Storage only — the live demo is NOT wired to the store in this step.
Focused suite 47 passed, full suite 858 passed (exit 0 each, captured
under `pipefail`); no camera opened, no enrollment/deletion run,
no biometric artifact accessed, created, or modified; nothing staged
or committed.**

**Implemented**

* `src/attendance_store.py` — `AttendanceEventStore`, an append-only
  JSON Lines journal over the existing frozen `AttendanceEvent` values
  at `<DATA_DIR>/attendance/events.jsonl` (path derived like every
  other storage root from `AppSettings`). Record schema is exactly five
  keys — `schema_version: 1`, `event_id`, `employee_id`,
  `event_type`, `occurred_at` (ISO-8601, UTC-normalized) — and the
  five-key envelope is *enforced on load*: extra keys (biometric,
  diagnostic, or future) are typed errors, never silently reinterpreted.
  No crops, images, embeddings, similarity scores, or unknown-person
  data are written or read by this module.
* Append policy: normal appends never rewrite the journal —
  `open("a")` + `flush()` + `os.fsync()` per record, so a record counts
  as persisted only after fsync returns; any directory/app.write/flush/
  fsync failure raises `AttendanceStorePersistenceError` and the
  caller must treat the event as **unconfirmed** (the module docstring
  and a pinned test state honestly that a record may still be present
  after an fsync failure — append cannot retract buffered data).
* Torn-tail policy (the documented recovery contract): an interrupted
  append can leave a final line without its newline. Loading excludes
  the torn fragment *and reports it* (`incomplete_trailing_record=True`)
  — corruption anywhere else always raises
  `CorruptAttendanceJournalError` with the exact line number; mid-file
  corruption is never silently skipped. Appending over a torn tail
  repairs it first: a trailing fragment that is a fully valid record
  is salvaged by a one-byte newline append (pure append; preserved);
  otherwise the complete-line prefix is republished through staged
  temp file → fsync → atomic `os.replace` (the Step 09 pattern) — the
  only rewrite path in the store, touching only the trailing
  fragment. A failed repair raises and leaves the journal byte
  unchanged (tested).
* Validation on load mirrors write: exact key set, integer (non-bool)
  schema version (`UnsupportedJournalSchemaVersionError` for unknown
  versions), known `EventType` value, parseable ISO-8601 aware
  timestamp, non-empty verbatim IDs — then the frozen
  `AttendanceEvent` constructor re-enforces the full event contract;
  malformed/older/wrong-typed records are typed errors carrying line
  numbers. A missing journal normalizes to empty history; a missing
  *mid-file* line does not exist as a concept — every complete line
  must validate.
* `src/config.py` — additive only: `AppSettings.attendance_dir`
  derived path (`data_dir / "attendance"`), mirroring
  `embeddings_dir`; no existing config object, validation, or
  `ensure_directories` entry changed (the store creates its directory
  lazily inside the first real append, like Step 09's storage).
* `tests/test_attendance_store.py` — 47 deterministic, hardware-free
  tests: single/multi append and in-order load; per-`EventType`
  round-trip; verbatim ID and exact-microsecond timestamp round-trip;
  offset-timestamp UTC normalization; load is read-only (byte
  comparison); construction performs no filesystem side effects;
  missing/empty journal = empty history; non-event append rejection
  with zero writes; fsync failure → typed persistence error with the
  unconfirmed-record honesty pinned; real (unmocked)
  directory-creation failure wrapped; privacy envelope (serialized
  records carry exactly the five documented keys); load validation
  matrix (malformed JSON / empty line / mid-file corruption with a
  torn tail present / unsupported or wrongly typed schema versions /
  missing or extra keys / unknown event types / naive or unparseable
  timestamps / empty or wrong-typed identity fields / non-object
  records / non-UTF-8 journals); incomplete trailing fragment
  (excluded and flagged on load; salvage-on-append of a
  complete-record fragment; truncate-on-append of garbage with the
  garbage provably gone; failed repair leaves journal bytes and
  staging cleanup verified; a subsequent append never concatenates
  onto a torn fragment); frozen explicit load result; the typed error
  hierarchy.

**Known limitations / honest boundaries**

* Not wired to the live demo or the engine in this step (P2-2 does
  that composition); nothing in the existing attendance behavior
  changed — all existing tests pass unchanged.
* No cross-process file locking and no `event_id` uniqueness scan: a
  retry after an uncertain append failure can produce two
  identical-ID lines; exactly-once/retry policy belongs to the
  composition layer. Concurrent appenders' interleaving is decided by
  the OS, not this store.
* fsync establishes file durability; no directory-entry fsync is
  performed (matching the existing storage precedent on the supported
  environments).
* No query/audit API yet — the journal is the retrieval source for
  later P2 steps; empirical CV evaluation remains separately BLOCKED
  (unchanged).

## Step 20 — unknown-face handling wired into live mode (2026-10-22)

**Report-only console wiring of the existing Step 12 handler; focused
suite 42 passed, full suite 811 passed (exit 0 each, captured under
`pipefail`); no camera opened, no live demo run, no biometric artifact
accessed, created, or modified; nothing staged or committed.**

**Implemented (composition only — no new policy, threshold, config, or
persistence)**

* `src/integration_demo.py` (`run_live`): the live loop now constructs
  the real `UnknownFaceHandler` once per session (frozen default
  `UnknownFaceConfig`: 30 s non-refreshing cooldown, `max_sessions` LRU
  bound, in-memory only) and feeds it every full-chain frame's temporal
  verification result through the authoritative `observe_temporal`
  contract — the exact pattern `run_smoke` already uses. Deterministic
  report-only console lines follow the smoke checkpoint vocabulary
  (`flush_unknown`): `unknown-face outcome observed (…)` on the first
  unresolved observation of an episode, `suppressed (…;
  suppressed_frames=N)` inside the cooldown, and `episode_ended
  (suppressed_frames=N)` when a temporally verified identity resolves
  the episode. `NO_ACTION` stays silent (including a verified identity
  with no active episode); the employee ID never appears on an
  unknown-face line.
* Scope boundary preserved honestly: frames that never reach the full
  chain (no faces, multiple faces, quality-rejected, or the partial
  no-sface demo) are not fed to the handler and never have temporal
  evidence fabricated for them — the live loop did not run the
  recognizer on such frames before this step and still does not, so
  existing streak semantics across skipped frames are unchanged.
* `run_live` gained a keyword-only `monotonic` clock parameter (same
  default-resolution convention as `run_smoke`) so hardware-free tests
  pin cooldown boundaries deterministically; production default is
  unchanged (`time.monotonic`).
* `tests/test_integration_demo.py`: 9 focused additions — the pure
  line formatter (observed/suppressed/episode-ended exact strings,
  count appendix, no-appendix case), the report helper against the
  real handler with a deterministic clock (observed once, repeats
  suppressed with the running count, verified identity resolves the
  episode with EPISODE_ENDED and without leaking the ID, NO_ACTION
  silence), and a full live-loop wiring test with injected doubles
  (no camera, no real model inference) asserting the exact console
  sequence OBSERVED → SUPPRESSED → SUPPRESSED → EPISODE_ENDED → fresh
  OBSERVED, exactly one attendance decision (verified frame only;
  unresolved observations can never authorize an event), and clean
  camera release. Attendance, recognition, temporal, quality,
  duplicate-suppression, and cleanup behavior are otherwise unchanged.
* README (live-mode section) and CHANGELOG (0.19.0 entry) document the
  live unknown-face reporting and its biometric discipline.

**Known limitations / honest boundaries**

* The wiring is verified by automated hardware-free tests only; a real
  supervised live run of the unknown-person episode remains the open
  operator rehearsal (consented, bounded `--max-frames`, empty store
  not required — a non-empty store with REH-001 is preferred so an
  unknown face is judged as UNKNOWN/AMBIGUOUS rather than
  NO_CANDIDATES). Nothing was run this step.
* Episode state is handler-instance lifetime (Step 12 semantics
  unchanged); there is no durable store, by design (Phase 2 owns
  durable unknown-face/attendance storage).

## Step 19 — camera frame-size override on the operator enrollment CLI (2026-10-21)

**Fix-record for a measured live-workflow blocker; automated checks
only (no camera opened, no enrollment run, no store touched).**

**Measured failure that motivated this step.** On the development
machine, `python tools/enroll_employee.py --employee-id REH-001 …
--confirm-consent` aborted before any frame ("frames attempted: 0";
"Nothing was published") and `tools/run_integration_demo.py --live
--camera-index 0` failed in `CameraService.open()`: the index-0
camera reports 640×480 while the configured default
(`src.config.CAMERA`) is 1280×720, so the open-time framing check
failed — exactly the loud validation behavior it was designed to
have (never silently accept a different resolution).

**Implemented (composition only; no default or validation change)**

* `tools/enroll_employee.py` gained an optional
  `--frame-size WIDTHxHEIGHT` override that reuses the live demo's
  validated parser (imported, not copied) and builds a temporary
  `CameraConfig` overriding only the requested frame width/height —
  the device index stays `CAMERA.index`, the configured default
  1280×720 is unchanged, and camera validation is untouched: the
  device must actually apply the requested size or acquisition fails
  with the existing typed `CameraOpenError`.
* `tests/test_enroll_employee_cli.py` extended with frame-size
  categories (parser surface incl. malformed/non-positive exit 2,
  camera-config composition incl. `CAMERA` default immutability, and
  flow/main wiring including the no-override path and duplicate
  refusal under the override) — all hardware-free and model-free by
  the suite's standing discipline.
* README (operator-enrollment section) and CHANGELOG document the
  option and its motivating measured failure.

**Honest boundary (unchanged).** This step adds an *explicit* way to
request a supported resolution; it does not weaken the resolution
validation, does not change the configured default, and does not
claim any new live verification — the supervised enroll → recognize
→ delete rehearsal remains the open live proof, now runnable on this
camera with `--frame-size 640x480` on both operator commands.

**Follow-up (same day): failure-output rejection-reason tally.**
After the resolution fix, a measured live enrollment attempt still
failed 0-samples-out-of-60 — and the CLI's failure line, unlike the
live demo's per-frame console output, hid *why* frames were rejected
even though `EnrollmentResult.rejections` already carried each
attempt's structured reason (log check confirmed no read/detection
session errors; the split was simply never printed). The failure
branch now appends a deterministic, biometric-free summary — counts
by stable reason value, lexicographically sorted (e.g.
`rejections by reason: blur=1, no_faces=2, too_small=1`) — omitted
when the session recorded no rejections, and extended with focused
CLI tests (multi-reason counts + sort, run-to-run determinism,
no-tally for an empty list, names-and-counts-only output).
Capture logic, thresholds, camera configuration, persistence, and
success behavior are unchanged; the tally is diagnostic only. The
*cause* of the 0/60 failure stays unrecorded until the next measured
attempt reads its own tally (run the supervised enrollment again now
that the diagnosis can come from the run itself, not from
hypotheses).

**Follow-up (same day): opt-in sharpness diagnostic on the live
demo.** A subsequent live run showed an intermittent `rejected by
quality: blur` pattern (some frames passing the whole gate and
reaching `recognition: no_candidates`), with the actual numeric
sharpness values computed by the real assessor but surfaced nowhere.
`run_integration_demo.py --live` gained an explicit,
disabled-by-default, console-only `--report-quality-metrics` flag:
frames rejected specifically for blur then also print the
already-measured sharpness and the configured threshold (numbers
only; never recomputed, never saved, never logged; flag-off output
byte-identical). Covered by hardware-free tests at both the CLI
wiring surface and the pure formatting surface, including the
unmeasured-sharpness invariant. No threshold, camera, recognition,
or dependency change; the 50.0 value remains uncalibrated until a
measured real-footage discussion in Phase 2 — the flag only makes
the values visible.

## Step 18 — status record (2026-10-20)

**Operator deletion CLI green (10 focused categories; full suite green;
no camera opened and no real biometric data created or deleted by this
step — all deletion evidence lives in temporary directories).**

**Implemented**

* `tools/delete_enrollment.py` — operator CLI removing exactly one
  employee's enrollment artifacts (embedding record pair, Step 07
  manifest directory, face-sample directory) using only public
  validation APIs (no invented deletion service): ID validated via
  `safe_dir_name` before anything runs; every removed path derived
  from the configured `AppSettings` roots with defensive containment
  checks; preview (default) changes nothing; `--execute` alone
  refused (exit 2); both separate flags trigger deletion and never
  bypass validation/target checks; removal order mirrors enrollment's
  publish order in reverse so a mid-deletion crash leaves only the
  publish-safe orphaned-samples state; failures report exactly what
  was removed and what remains; success requires provable absence via
  the real `EmbeddingStorageService.list_employee_ids()` plus direct
  artifact checks. Crops/vectors never read, printed, or logged.
* `tests/test_delete_enrollment.py` — 10 focused categories (tmp-dir
  artifacts + injected storage double; no camera, no real biometrics):
  invalid/malicious ID, nonexistent enrollment, preview-is-const,
  missing individual artifacts, confirmation refusal, full deletion,
  deletion failure with partial reporting, final-verification
  failure, neighbor/model isolation, success-only-on-absence.
* README "Deleting an enrollment" subsection: exact commands,
  deleted-artifact enumeration, confirmation semantics, crash-safe
  ordering, recovery limitation (irreversible).

**Verified**

* Focused deletion tests + prior suites (enrollment bridge, CLI,
  smoke) and the full suite exited 0; counts and exit statuses
  captured under `pipefail` (recorded in the change history).
* Live boundary runs against the real (empty) store: preview and the
  execute-without-confirm refusal both behave as documented on the
  real `data/` without touching anything real. `git diff --check`
  clean; nothing staged or committed.

**Known limitations / honest boundaries**

* Deletion is irreversible (no trash/versioning): recovery requires
  re-enrolling a re-consenting participant — documented.
* Automated tests prove the tool's filesystem scoping and wiring with
  synthetic artifacts in temporary directories; no real enrollment has
  ever been deleted on this machine (the stores were verified empty).
* The recognizer's candidate list reflects complete records only;
  final verification therefore checks both the discovery API and the
  literal files, so a corrupt half-record cannot masquerade as a
  deleted one.

## Live partial-run measurement (2026-10-20, 17:56–17:57) — log-backed

Supersedes, for the live *acquisition chain* only, the "model loading
only until a real supervised camera run is recorded" boundary recorded
at Step 15. Evidence: `data/logs/app.log`, entries 2026-10-20
17:56:19–17:56:51 and 17:57:11–17:57:37 (quoted facts only; no
biometric content is ever logged).

**Measured (log-backed):**

* Two real `--live` sessions ran on the development machine.
* The real YuNet detector loaded both times from the
  operator-supplied `models/yunet.onnx` (`score_threshold=0.50`, the
  configured constant).
* Real camera hardware acquired (index 0, 640×480) and **released
  cleanly** via the project's guaranteed resource-safety path after
  ~28 s and ~25 s sessions.
* The real SFace embedding model loaded in crop-only mode (no
  landmark alignment) — the same defaults the recognizer uses.
* **Zero ERROR and zero WARNING log entries** across both sessions:
  no unexpected exception occurred, both cameras released cleanly.

**Not measured / did not occur in these sessions:**

* **No enrollment was performed** (no `enrollment persisted` or
  `embedding persisted` log entries; the employee/face-sample/
  embedding stores were verified still empty immediately after), so
  **no recognition or attendance decision was possible**: no
  `no_candidates`/`matched`/`VERIFIED` console line exists in any
  record, and **real-camera recognition accuracy remains unrecorded**.
* These sessions therefore verify the live prerequisite and resource
  chain (operator-supplied models load, real device acquires and
  releases, no exceptions) — not recognition quality, temporal
  verification, or attendance behavior.
* Still open: a supervised consented enroll → bridge → recognize
  (`VERIFIED` event) → delete cycle, and accuracy evaluation (FAR/FRR
  on labeled data; spoof resistance; multi-person scenes) — blocked
  exactly as recorded in Step 14.

## Step 17 — status record (2026-10-20)

**Operator enrollment CLI green (8 focused categories; full suite green;
no camera opened and no biometric data created by this step; live
recognition still not exercised — unchanged boundary).**

**Implemented**

* `tools/enroll_employee.py` — operator CLI connecting the existing
  Step 07 enrollment to the Step 16 embedding bridge (composition
  only; no new recognition math, thresholds, persistence, camera, or
  model behavior). Mandatory `--confirm-consent` gate placed *before
  everything* (no prerequisite probe, no camera, no validation — exit
  2 without it); prerequisite probing aborts before any capture when
  the operator-supplied model artifacts are absent; the existing
  enrollment session's duplicate refusal and typed errors surface
  unchanged; the bridge creates exactly one embedding under its
  `replace=False` contract; success is printed only after the employee
  ID is provably discoverable through
  `EmbeddingStorageService.list_employee_ids()` — the live
  recognizer's actual candidate path. Biometric discipline:
  crops/vectors/embeddings never printed or logged. Failures are
  reported honestly (duplicate → refused, nothing changed; embedding
  failure after capture → published enrollment remains on disk with
  the printed recovery path), never fabricated as success.
* `tests/test_enroll_employee_cli.py` — 8 focused categories
  (consent declined runs nothing; consent accepted runs enroll→bridge
  in order; real-validation ID/name rejection before any camera;
  duplicate enrollment and duplicate-embedding refusals;
  capture/enrollment failure yields no embedding work;
  embedding/storage failure; final candidate-verification failure;
  success only on discoverable ID) — all injected doubles, no real
  camera, models, or biometric data.
* README "Operator enrollment" section: exact command, prerequisites,
  consent requirement, duplicate behavior, partial-failure recovery,
  and biometric-output discipline.

**Verified**

* `python -m pytest -q`: **746 passed**, exit 0 — including the 17
  focused CLI tests, the 16 bridge tests, and the Step 07
  enrollment suite (54 across both). Exit statuses captured under
  `pipefail`; the two test-fix cycles this step needed (a
  probe-shaped test double and a closure-scoped calls recorder in the
  new test file; no source-code logic changed) were fixed at their
  causes, no assertion weakened, no test skipped.
* `git diff --check` clean (exit 0; benign LF→CRLF working-copy
  warnings on stderr only). No biometric data created; the employee,
  face-sample, and embedding stores remain empty because no real
  enrollment was performed during implementation.

**Known limitations / honest boundaries**

* Automated tests exercise wiring with injected doubles; they do not
  and cannot prove real-model recognition accuracy. The only remaining
  end-to-end proof is an authorized, consented, supervised live run
  after a real enrollment.
* There is no programmatic "delete enrollment" command: duplicate
  under the same ID requires manual removal of that employee's data
  directories (documented recovery path; duplicates are never
  overwritten).
* Consent is enforced by the operator's `--confirm-consent` flag on
  the command line; verifying the participant's actual consent remains
  a human responsibility the flag represents.

## Step 15 — status record (2026-10-14)

**Integration & automated smoke-test status: green (20 focused tests
passed; 674/674 full suite, project root and external CWD). Actual
live-camera results: not obtained (no camera exercised this step).
Empirical computer-vision accuracy: still BLOCKED exactly as recorded
in Step 14 — integration passing does not validate real-world CV
performance.**

**Implemented**

* `src/integration_demo.py` — the Phase 1 integration orchestrator:
  it *composes* the existing approved services (Steps 04–13) along
  capture → detect → quality → embed → recognize → temporally verify
  → unknown-face handling → attendance event, adding **no** new
  recognition math, quality metrics, temporal policy, or thresholds.
  Trust boundaries preserved: only a real ``VERIFIED``
  temporal-surface identity reaches the real attendance engine; raw
  or unverified recognition can never authorize an event; unknown-face
  handling stays observational.
* `run_smoke` — deterministic,
  hardware-free demo: injected frame source / scripted detector /
  stub embedder at the module boundaries; the *real* embedding
  storage, recognition service, temporal verifier, unknown-face
  handler, and attendance engine run with unchanged contracts over
  five scripted frames. Builds its Step 07-shaped manifest + Step 09
  embedding fixture in temp directories (never the real `data/`).
  One real `CHECK_IN` event for the exact ID `"007"` (streak of 3
  consecutive MATCHED frames, cosine 1.0 on the synthetic vectors),
  plus 2 demonstrated engine suppressions for repeat-verified frames,
  and the full unknown-face episode lifecycle (observed → suppressed
  → episode_ended). Scenarios that gate attendance (quality
  rejection, no faces, off-basis/unknown recognition) return an
  honest `event_created=False` result — nothing fabricated
* `run_live` (opt-in) — real-camera pipeline: prerequisite probing
  before any hardware is acquired; `yunet.onnx` missing → clean
  abort; `sface.onnx` missing → clearly
  labeled *partial* live demo (capture → detection → quality only)
  that **never** generates attendance events; camera open/read
  failures, window closure, `KeyboardInterrupt`, and unexpected
  exceptions shut down with camera/window/capture release in a
  `finally` block.
* `tools/run_integration_demo.py` — CLI (`--smoke` default,
  `--live` opt-in with `--camera-index/--no-window/--max-frames`,
  optional `--frame-size WIDTHxHEIGHT` override);
  `--help` and `--smoke` verified working.
* `tests/test_integration_demo.py` — 20 focused tests: happy path
  (one event for exact ID; per-stage checkpoint coverage; 2
  suppressions; VERIFIED temporal detail), boundary-injection
  integrity, no-event guarantees for quality-rejected / no-face /
  unknown-evidence runs, unknown-face stage follows the real
  handler, real `data/` untouched (before/after listing), fixtures
  confined to tmp, typed input errors, live abort/prerequisite and
  headless-window paths, and README-snippet sync.

**Verified**

* Full suite baseline before this step's code changes: **674
  passed**, exit 0 (re-run; the project was already green).
* After changes — root: **674 passed**, exit 0; from an external CWD
  (`/tmp/empty-cwd-15`, same venv, project tests dir): **674 passed**,
  exit 0.
* `python tools/run_integration_demo.py --help` → usage text, exit 0;
  `python tools/run_integration_demo.py --smoke` → event for `"007"`,
  2 suppressions, full checkpoint trace, exit 0.
* The one suite failure encountered (a `src/` hygiene guard forbidding
  any reference to the model-download helper from startup code) was
  fixed by removing the docstring reference — the guard's rule stands
  intact, no test was weakened.

**Known limitations / honest boundaries**

* At Step 15 recording time no real camera and no real SFace
  inference were exercised: `models/sface.onnx` was absent, so live
  mode could only run the partial demo. The operator has since
  provided `models/sface.onnx`, which enables `--live`'s full-chain
  path — but that path is model loading only until a real supervised
  camera run is recorded; recognition accuracy claims remain
  unfabricated and unmade.
* Smoke mode's synthetic vectors (cosine 1.0) demonstrate pipeline
  correctness only — never recognition accuracy.
* Duplicate suppression remains engine-instance-lifetime (Step 13
  restart limitation, unchanged); no durable store was added.
* Live mode without a face tracker cannot distinguish two people in
  one camera session (documented Phase 2 tracker work).

## Step 14 — status record (2026-10-13)

**Software regression status: green. Empirical CV evaluation status:
BLOCKED (missing artifacts/data — recorded honestly, not a code
failure). Step 14 is NOT claimed fully passed.**

**Implemented**

* `notebooks/09_phase1_evaluation.ipynb` — reproducible, privacy-safe
  evaluation notebook (nbformat 4; 20 cells / 11 code cells) covering
  the Step 14 scope: objectives & scope, environment summary (python
  /platform/numpy/opencv, frozen component defaults), artifact
  availability (yunet.onnx PRESENT, sface.onnx MISSING — nothing ever
  downloaded), safe regression-test instructions (project root
  `python -m pytest -q`, external-CWD command; opt-in
  `RUN_FULL_TEST_SUITE` runner cell), software-level detection checks
  (box-contract validation, documented IoU matching helper) with an
  **explicitly opt-in** local YuNet smoke cell, synthetic-input
  quality-threshold rows plus the frame face-count policy,
  synthetic-identity recognition decision rows, temporal-verification
  / unknown-face / attendance behavior rows (fake clocks, no
  sleeps), machine-dependent software-overhead timing, and a
  limitations/spoof-boundary/conclusions section. Every cell: no
  camera, no enrollment, no production-`data/` mutation, no biometric
  reads, no downloads.
* `reports/phase1_evaluation.md` — evidence-based report in which
  every value is labeled MEASURED / TEST COVERAGE / NOT RUN /
  BLOCKED / NOT MEASURED with the specific reason: regression
  commands and counts; artifact table; detection (IoU criterion
  pre-defined; precision/recall NOT RUN — no annotated dataset);
  synthetic quality rows (incl. the documented first-failed-check
  semantics and the pose-proxy boundary); recognition decisions on
  synthetic vectors with FAR/FRR NOT MEASURED (no sface.onnx, no
  labeled data); temporal/unknown/attendance behavior rows incl.
  exact cooldown boundaries and inactive gating; performance
  (YuNet ~7–11 ms median on 240×320 synthetic frames; cosine ~16–34
  µs; assess_face ~150–171 µs; temporal update ~5 µs —
  machine-dependent); the documented-but-not-run spoof scenario
  plan (no spoof-resistance claim); risks; reproduction
  instructions.

**Verification (run 2026-10-13, exit 0)**

* Baseline before changes: 654 passed (root), exit 0; baseline run
  re-established rather than assumed.
* Notebook structural validation: valid JSON, nbformat 4; **all 11
  code cells executed clean** in sequence (runner with per-cell
  exit-status capture); the run also demonstrated end-to-end
  fail-safe behavior on real local code paths.
* `git diff --check` clean; no model files, datasets, biometric
  samples, or persistent attendance files added by this step.

**Blocked/boundary records (carry forward):** detection
precision/recall on annotated data — NOT RUN (no dataset);
recognition accuracy FAR/FRR — NOT MEASURED (sface.onnx absent);
real-lighting/pose/occlusion and spoof behavior — NOT RUN (no consented
images, no liveness component, no authorized rig); end-to-end camera
latency — deferred to Step 15 integration. These are environment
blocks, not software failures.

## Step 13 — completion record (2026-10-13)

**Implemented**

* `src/attendance.py` — `AttendanceEngine`: deterministic,
  independently testable attendance-event engine building strictly on
  the approved Step 11 trust boundary (accepts only
  `TemporalVerificationResult`; a raw `RecognitionResult` is never an
  accepted input — a `MATCHED` recognition result alone cannot
  authorize an event). No matching, threshold, embedding, manifest,
  camera, persistence, check-out/shift/lateness/payroll, audit-trail,
  or camera-loop code; the identity/recognition/attendance split is
  preserved.
  * **Trust boundary (enforced):** only a structurally valid
    `VERIFIED` temporal result authorizes a decision;
    `COLLECTING`/`INTERRUPTED` yield `NOT_VERIFIED` (no event);
    malformed inputs (wrong type, synthetic bogus status,
    `VERIFIED` without a valid non-whitespace employee ID) fail
    safely with typed `AttendanceInputError` — no event, no state
    change, no fabricated success. IDs preserved exactly
    (`"007"`, `"EMP-0042"`); inputs are never mutated.
  * **Active gate (no invention):** optional injectable
    `active_lookup` (the same hook pattern Step 10 recognition
    already uses — no new manifest or storage reads) reports the
    verified employee inactive → `INACTIVE_EMPLOYEE` decision, no
    event; a non-bool return fails safely. With no hook, the engine
    invents no active status — gating is delegated upward.
  * **Event schema (frozen, minimal):** `AttendanceEvent(event_id,
    employee_id, event_type, occurred_at)` with `EventType.CHECK_IN`
    only; `occurred_at` requires a timezone-aware datetime and is
    normalized to UTC at construction (naive datetimes rejected);
    `event_id` defaults to `uuid.uuid4().hex` (documented,
    validated, injectable factory). No images, crops, embeddings,
    similarity diagnostics, or unknown-person info.
  * **Decision API:** frozen `AttendanceDecision(status, event,
    reason)` with `AttendanceDecisionStatus.{EVENT_CREATED,
    NOT_VERIFIED, INACTIVE_EMPLOYEE, SUPPRESSED}` — `event` is
    non-`None` **only** for `EVENT_CREATED`; suppressed/unverified
    decisions never expose a fabricated event.
  * **Duplicate suppression (in-memory, bounded, non-refreshing):**
    one `CHECK_IN` per employee per
    `AttendanceConfig.check_in_cooldown_seconds=300.0`; the anchor is
    the created event's instant on an injectable **monotonic** clock
    (default `time.monotonic`); boundary exact (elapsed < cooldown
    suppressed; == cooldown → new event); suppressed repeats never
    refresh the anchor. Different employees independent; anchors
    bounded by `max_tracked_employees=64` with LRU eviction (worst
    case: one extra event). **Restart limitation documented:**
    duplicate protection lives only for the engine-instance
    lifetime — this is NOT durable storage, database-level, or
    cross-process exactly-once; a process restart loses anchors.
  * **Clock separation (documented, tested):** `clock_wall`
    (timezone-aware datetimes; naive/non-datetime outputs rejected)
    produces event timestamps; `monotonic_clock` governs suppression
    (non-finite/non-numeric outputs rejected) — backward wall-clock
    movement can neither bypass duplicate protection nor expire
    anchors; no contradictory decision paths.
* `src/config.py` — frozen `AttendanceConfig` + `ATTENDANCE`:
  `check_in_cooldown_seconds=300.0`, `max_tracked_employees=64` —
  **initial engineering values**, not calibrated workplace policies
  (Phase 2 business work); strict validation (bool/float/str
  cooldowns rejected, NaN/inf/non-positive rejected, int counts
  only). No existing config object changed.
* `tests/test_attendance_engine.py` — 52 deterministic,
  hardware-free tests (synthetic temporal results, injected fake
  wall+monotonic clocks, injected event-ID factories; no sleeps/no
  camera/model/network/biometric files) covering all 30 Step 13
  checklist items: the full trust boundary (verified vs
  COLLECTING/INTERRUPTED vs raw recognition vs malformed), event
  schema (immutability, exact IDs, UTC normalization, unique
  uuid-hex IDs, no biometric fields), suppression semantics (first
  eligible, within-interval suppression, per-employee independence,
  exact boundary, non-refreshing repeats, re-eligibility after
  expiry), invalid config values/types, invalid clock outputs
  (incl. backward wall time), reset (single/all/invalid), instance
  isolation, factory-failure atomicity, LRU-bounded state, and a
  fresh-subprocess foreign-CWD import/construction side-effect
  check.

**Verification (run 2026-10-13, exit 0)**

* Focused: `pytest tests/test_attendance_engine.py` → 52 passed.
* Full suite, project root: **654 passed** (602 + 52).
* Full suite from an external working directory: 654 passed.
* `git diff --check` clean; no generated artifacts, biometric
  samples, or persistent attendance files added.

**Honesty note.** No real-camera accuracy, no durable storage, and no
production-readiness claim: duplicate protection is engine-instance-
lifetime only. Event timestamps/suppression are unit-verified against
injected clocks, not validated against production deployment
timing. Check-in-only; business rules (check-out, shifts, lateness,
daily dedup, payroll) and a future durable event store are
Phase 2 / later approved steps.

## Step 12 — completion record (2026-10-13)

**Implemented**

* `src/unknown_face_handler.py` — `UnknownFaceHandler`: conservative,
  in-memory unknown-face handling built **on** the approved Step 10
  (`RecognitionResult`) and Step 11 (`TemporalVerificationResult`)
  contracts. It never identifies an unknown person beyond the existing
  recognition service, never records attendance or generates events,
  performs no persistence, tracking, notifications, or network calls,
  and never bypasses `TemporalVerifier`.
  * **Two documented entry points (trust boundary):**
    `observe_temporal(result, session_key)` is the **authoritative**
    path — a `VERIFIED` temporal result is the only signal treated as
    a temporally verified identity (yielding `NO_ACTION`, or
    `EPISODE_ENDED` when an unresolved episode was active); the
    outcome carries **no** identity. `COLLECTING`/`INTERRUPTED` are
    handled conservatively as unresolved observations (possibly an
    enrolled employee mid-verification, possibly a stranger — never
    guessed). `observe_recognition(result, session_key)` is the
    **observability** path — `UNKNOWN`/`AMBIGUOUS`/`NO_CANDIDATES`
    classify one-to-one; a raw `MATCHED` is classified
    `MATCH_UNVERIFIED` (a single-frame match is explicitly NOT a
    temporally verified identity). No entry point can turn any
    recognition result into an identity or attendance action.
  * **Outcome types (frozen):** `UnknownFaceOutcome(status,
    observation, suppressed_frames, reason)` with
    `UnknownFaceStatus.{NO_ACTION, OBSERVED, SUPPRESSED,
    EPISODE_ENDED}` and `UnresolvedObservation.{UNKNOWN, AMBIGUOUS,
    NO_CANDIDATES, MATCH_UNVERIFIED, TEMPORAL_UNVERIFIED}` —
    statuses/counts/reasons only, no employee IDs, scores, frames, or
    crops ever carried.
  * **Cooldown / dedup policy (deterministic):** the first unresolved
    observation in a session opens a bounded episode (`OBSERVED`);
    further unresolved frames within
    `UnknownFaceConfig.episode_cooldown_seconds` of the alert instant
    are `SUPPRESSED` (counter reported). The cooldown is
    **non-refreshing** — the episode expires exactly at
    alert + cooldown (lazy, checked against an injectable monotonic
    clock), so a later unresolved frame after expiry opens a fresh
    episode: stale state can never suppress observations
    indefinitely. `reset(session_key=None)` ends one or all episodes
    and returns how many were active. No sleeps in tests (fake clock).
  * **Bounded state (no face-track identity claims):** per caller-
    managed session key, at most `UnknownFaceConfig.max_sessions`
    episodes retained with deterministic LRU eviction (worst case
    after eviction: one extra alert). Suppression is per session,
    and the project has no stable face-track identifier — suppression
    is a bounded unresolved-session policy, NOT proof that repeated
    frames show the same person; multiple unknown people within one
    caller session cannot be distinguished (documented limitation; no
    tracker added). Session keys: non-empty str, capped length
    (strict; whitespace-only/oversized keys rejected, never
    truncated); `reset(None)` is the documented clear-all call.
  * **Fail-safe inputs:** typed `UnknownFaceInputError` for wrong
    result types, synthetic bogus statuses (the Step 10/11 dataclass'
    do not type-check their status fields), and contract violations
    (`MATCHED`/`VERIFIED` without a valid identity) — inconsistent
    results never become observations, identities, or suppressed
    state; the handler stays usable afterwards.
* `src/config.py` — frozen `UnknownFaceConfig` + `UNKNOWN_FACE`:
  `episode_cooldown_seconds=30.0`, `max_sessions=8`,
  `max_session_key_length=128`. All **initial engineering defaults**,
  not calibrated operational or security guarantees (Phase 2
  calibration); strict validation (bool is rejected for every
  numeric field; NaN/inf/non-positive cooldowns rejected; ints only
  for counts). No existing config object changed.
* `tests/test_unknown_face_handler.py` — 56 deterministic,
  hardware-free tests (synthetic results + injected fake clock; no
  camera/model/network/biometric files) covering the full Step 12
  checklist and edge cases: verified-vs-unverified boundaries for
  every temporal/recognition status, one-to-one status
  classification with no identity leakage, cooldown boundaries
  (suppression window, exact expiry at alert+cooldown,
  non-refreshing semantics), episode end via verified identity and
  via reset (single/all), expiry after long stale silence, bounded
  state (long runs, LRU eviction, no unbounded growth), instance and
  session isolation, session-key validation (incl. bool/float
  cooldowns, key types/length), frozen outcome/config types, contract
  immutability after observation, and a fresh-subprocess foreign-CWD
  import/construction side-effect check.

**Verification (run 2026-10-13, exit 0)**

* Focused: `pytest tests/test_unknown_face_handler.py` → 56 passed.
* Full suite, project root: **602 passed** (546 + 56).
* Full suite from an external working directory: 602 passed.
* `git diff --check` clean; no generated artifacts, no biometric
  data, no images, no model files, no persistent logs added.

**Honesty note.** Deterministic in-memory logic only: no real-camera,
no real-recognition-pipeline, and no operational or security
calibration was performed. Unknown-face handling is **not** an
attendance event and stores no audit trail — any future persistent
audit/event design belongs to a later approved step. Suppression is
per caller-managed session, not per person (no stable tracking
exists).

## Step 11 — completion record (2026-10-13)

**Implemented**

* `src/temporal_verification.py` — `TemporalVerifier`: deterministic
  multi-frame temporal verification built **on** the Step 10 result
  contract (consumes `RecognitionResult` values one at a time; never
  duplicates cosine similarity, embedding loading, manifest
  validation, or recognition thresholds). **Temporal state only** —
  no single-frame recognition replacement, no attendance event
  generation, no face tracking, no persistence, no camera, no model,
  no network, no writes.
  * **Evidence policy (conservative):** only `MATCHED` frames with a
    matched employee ID count as positive evidence, and only for the
    **same** identity — cross-ID evidence is never combined, and IDs
    are preserved verbatim (`"007"`, `"EMP-0042"`, punctuation). Any
    non-MATCHED frame (`UNKNOWN` / `AMBIGUOUS` / `NO_CANDIDATES`)
    **interrupts** the accumulated streak (`INTERRUPTED`,
    `employee_id=None`, streak reset to 0); uncertain instants can
    never contribute to or preserve a verification. A matched frame
    for a different ID likewise discards the old streak and starts a
    new candidate streak from zero.
  * **Verification rule:** `VERIFIED(employee_id)` exactly when the
    uninterrupted same-ID matched streak reaches
    `min_consistent_frames`; below that, `COLLECTING` (no identity
    reported). Repeated processing after verification is
    **idempotent** — the verifier reports current temporal state, no
    per-frame "verification events" are emitted (callers that need
    event-on-transition semantics track transitions themselves).
    Verification cannot survive without new evidence: the very next
    non-MATCHED frame drops back to `INTERRUPTED`.
  * **Bounded state:** retained evidence is the streak (one ID + one
    counter), the last reason, and a bounded deque of at most
    `window_frames` recent statuses (deterministic oldest-first
    advance). No frames, crops, embeddings, scores, or names are
    retained; logs carry counters/statuses/reasons only, never IDs
    or biometric data.
  * **One sequence per instance:** the verifier manages a single
    tracking sequence — the caller creates one instance per tracked
    face / camera session; instances share no state (`reset()` is
    instance-scoped and clears streak + window).
  * **Types:** frozen `TemporalVerificationResult(status,
    employee_id, streak_frames, window_frames_used, reason)` with
    `TemporalVerificationStatus.{COLLECTING, VERIFIED,
    INTERRUPTED}`; typed `TemporalVerificationError` and
    `TemporalVerificationInputError` (includes refusal of `MATCHED`
    results with no valid `employee_id` — a Step 10 contract
    violation is surfaced, never silently de-escalated).
* `src/config.py` — frozen `TemporalVerificationConfig` +
  `TEMPORAL_VERIFICATION`: `min_consistent_frames=3` and
  `window_frames=10`. Both are **initial engineering defaults**, not
  calibrated production values — they must be tuned with real camera
  footage in Phase 2. Strict validation (plain ints only — bools and
  floats rejected; `window_frames >= min_consistent_frames` so
  verification is constructible) fails fast. No existing config
  object changed.
* `tests/test_temporal_verification.py` — 51 deterministic,
  hardware-free tests (synthetic `RecognitionResult` values only; no
  camera, no model, no network, no biometric files) covering the
  full Step 11 checklist: initial state, threshold-before-
  verification, eventual verification, cross-ID isolation, exact ID
  preservation, per-status (UNKNOWN/AMBIGUOUS/NO_CANDIDATES)
  non-evidence + interruption policy, window bounds/advance
  determinism, no stale verification, reset/scoping,
  instance isolation, config validation (incl. bool/float
  rejection), malformed-input handling, boundary behavior
  (`min_frames=1`, `window==min_frames`), bounded retention,
  frozen/immutability guarantees, and a fresh-subprocess
  foreign-CWD check that import + construction produce no side
  effects.

**Verification (run 2026-10-13, exit 0)**

* Focused: `pytest tests/test_temporal_verification.py` → 51 passed.
* Full suite, project root: **546 passed** (495 + 51).
* Full suite from an external working directory: 546 passed.
* `git diff --check` clean; no generated artifacts, no biometric
  samples, no model files added.

**Honesty note.** Deterministic temporal logic only: no real-camera,
no real-recognition-pipeline, and no accuracy calibration was
performed. Thresholds need Phase 2 calibration. Temporal verification
is **not** an attendance event — recording arrives in Step 13.

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
| 09 | Embedding storage | ✅ Complete | Verified 2026-10-12 — 421/421 tests pass (79 new embedding-storage tests). Details below. |
| 10 | Face recognition | ✅ Complete | Verified 2026-10-12 — 495/495 tests pass (74 new recognition tests); real SFace inference remains, by recorded honesty, in-suite-unverified. Details below. |
| 11 | Temporal / multi-frame verification | ✅ Complete | Verified 2026-10-13 — 546/546 tests pass (51 new temporal-verification tests). Details below. |
| 12 | Unknown-face handling | ✅ Complete | Verified 2026-10-13 — 602/602 tests pass (56 new unknown-face-handler tests). Details below. |
| 13 | Attendance event engine | ✅ Complete | Verified 2026-10-13 — 654/654 tests pass (52 new attendance tests). Details below. |
| 14 | Testing & evaluation | ◐ Software regression green; empirical CV eval BLOCKED | Regression suite green and all 11 notebook code cells executed clean (2026-10-13); detection/recognition accuracy on real footage NOT RUN / NOT MEASURED (no annotated dataset, no sface.onnx at recording time) — recorded honestly, not a code failure. Status record below. |
| 15 | Phase 1 integration demo | ◐ Integration & smoke green; real-camera demo not exercised at recording time | Verified 2026-10-14 — 674/674 full suite (root + external CWD), 20 focused smoke tests; the live acquisition chain was later log-backed 2026-10-20 (models load, camera acquires/releases — record above). Status record below. |

(Rows 09–15 above were reconciled on 2026-10-22 from the authoritative
per-step records below; a stale snapshot had erroneously shown them
"Not started".)

## Phases 2–7

*(Reconciled 2026-10-24: this summary previously stated the phases had
not started; the authoritative per-step records below it show otherwise
for Phase 2's durable track. Corrected to match Step 25.)*

**Phase 2 (attendance/business rules) — partially started** as an
approved, narrower durable-storage + inspection + wiring track:

* ✅ Completed and verified: Step 21 / P2-1 durable event store,
  Step 22 / P2-2 durable composition, Step 23 / P2-3 restart recovery,
  Step 24 / P2-4 read-only report CLI, and Step 25 / P2-5 opt-in
  durable live/smoke wiring — the authoritative per-step records below
  document each with its verification evidence.
* ✅ Still NOT started (separate milestone, sequence unchanged):
  check-out events, shift rules, lateness, daily deduplication, and
  wider business rules — no documented design and no implementation
  yet, by explicit owner decision.
* ◐ Step 14 (empirical CV evaluation) remains BLOCKED on consented
  labeled data and a real `models/sface.onnx` run.

Remaining phases — all not started (intentionally, sequentially):

PostgreSQL + FastAPI · Web dashboard · Security/RBAC/audit ·
Docker/deployment · Advanced liveness + optimization.

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
