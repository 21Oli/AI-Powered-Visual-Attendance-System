# DB_DESIGN.md — Phase 3 employee-metadata database design (Step 1.5a)

**Status: planning record from Step 1.5a; Sections 3, 4, and 5's
offline contracts were subsequently implemented in Step 1.5b (see the
Step 1.5b update note below each affected section and
DEVELOPMENT_STATUS.md Step 30).** Written in Step 1.5a (planning gate)
against baseline `bb95559` on `main`
(clean, synchronized with `origin/main`), after inspecting the actual
repository code, tests, and authoritative documents
(`PROJECT_PLAN.md`, `ARCHITECTURE.md`, `DEVELOPMENT_STATUS.md`,
`README.md`, `CHANGELOG.md`, `.env.example`, `src/*.py`, `tests/*.py`).

**Step 1.5b update (2026-11-02):** the owner approved all seven OQ-
1..7 recommendations, and the offline core was implemented and
offline-tested (`src/db_migrate.py`, `migrations/V001__employee.sql`,
`src/employee_repository.py`, `src/test_database_guard.py`, frozen
`PaginationConfig` in `src/config.py`; **70 new offline tests** before
the audit repairs below, **113** after them, **126** after the Option B
lock-lifecycle tests of Step 31). Still **not implemented
and not verified against a real PostgreSQL server**: the real
psycopg-backed `MigrationExecutor`, the no-serve-during-migration
runtime wiring, and any Tier-2 integration test (requires the
owner-provisioned dedicated test database with an established marker,
Step 1.5b-3). The employee-metadata exporter, importer and their
psycopg-backed repository wiring **are** implemented, but offline only
(Sections 9–11); the live import remains Step 1.5c-3.

**Audit-repair update (Step 1.5b follow-up):** an independent read-only
audit of the 1.5b work found real defects, now fixed offline —
psycopg-3 duplicate classification (`sqlstate`, not psycopg2's
`pgcode`), `CREATE UNIQUE INDEX CONCURRENTLY` escaping the prohibited
statement scan, `hostaddr`/`service` identity ambiguity, read-only mode
not actually enforced, a leaked verification connection, line-ending
sensitive checksums, and documentation/test-clarity errors. The
advisory-lock mechanism (OQ-1) is **reopened for owner decision**: see
Section 3.6 and `DEVELOPMENT_STATUS.md` Step 30.

## 0. Provenance

The Phase 3 architecture decision record was approved in the project's
owner sign-off conversation, which is not stored in this repository.
Decisions originating there are recorded here **as supplied**, not
independently verified from repository files. Items verified directly
against in-repo code/tests are marked **[REPO]** with the evidence cited.
Sections 3.6, 5.3, and 8 list items that were **not** already approved
and require an owner decision before the implementation gate.

## 1. Provenance table of decisions this plan relies on

| # | Decision | Provenance | Repository evidence |
|---|---|---|---|
| D1 | JSONL journal (`<DATA_DIR>/attendance/events.jsonl`) is the sole authoritative attendance journal; PostgreSQL stores **employee metadata only** | Approved (conversation decision record) | [REPO] `src/attendance_store.py` header ("storage only"; five-key privacy contract); `src/db.py` docstring ("PostgreSQL employee metadata only") |
| D2 | `Employee.created_at` is application-authoritative, constructed via `datetime.now(UTC)`, stored as `TIMESTAMPTZ NOT NULL` **without a database default** | Approved (conversation) | [REPO] `src/employee.py` `__post_init__` (sets `datetime.now(UTC)` when `created_at is None`; naive datetimes rejected) |
| D3 | API is loopback-only, initially unauthenticated, no permissive CORS; single-process guard uses an OS `msvcrt.locking` byte-range lock on one byte at offset 0 of `DATA_DIR/api_guard.lock` | Approved (conversation) | [REPO] `src/config.py` `_validate_api_host_value` loopback-only validation; `src/api_guard.py` full contract; `tests/test_api_guard.py` 14-test matrix |
| D4 | DSN redaction is key-aware over `password` and `sslpassword`; `passfile` deliberately unmasked (a path, not a secret) | Approved (conversation) | [REPO] `src/db.py` `_SECRET_KEYS` and `_redact()`; `tests/test_db.py` redaction cases |
| D5 | Operational rollback is non-destructive; destructive DROP/rebuild only on a positively verified disposable test database | Approved (conversation) | no migration code exists (Section 3 defines the contract) |
| D6 | `ATTENDANCE_TEST_DATABASE_URL` integration tests positively verify the target (including the `phase3_test_guard` marker) before any destructive setup; ambiguous → fail closed | Approved (conversation) | no integration test exists; `.env.example` documents the variable but states no enforcement exists yet |
| D7 | Exactly eight approved `/api/v1` endpoints; duplicate employee → 409; missing → 404; PATCH `full_name` only in v1; inactive employees cannot check out; checkout returns 201 only on durable append; `PERSISTENCE_REJECTED` ≠ journal-unchanged (directory creation/torn-tail repair may already have happened); `PERSISTENCE_UNCONFIRMED` never auto-retried; machine-readable decision codes separate from messages | Approved (conversation) | [REPO] `src/api.py` route inventory (seven routes currently answer 501 `NOT_IMPLEMENTED`); `src/attendance_persistence.py` outcome taxonomy; `src/attendance.py` `INACTIVE_EMPLOYEE` decision |

## 2. Approved employee table contract (Section A)

```sql
CREATE TABLE employee (
    employee_id TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL          -- application-authored; NO default
);
```

* PostgreSQL's Phase 3 payload is exactly these four metadata fields.
  **No** biometric columns, **no** embedding columns, **no**
  attendance-event tables, and no archival/biometric side tables are
  ever added in this phase (D1).
* `created_at` is supplied by the application from the already-validated
  `Employee.created_at` value — never a DB default, never a
  `NOW()`/session-timestamp substitution. The repository inserts the
  model value verbatim (D2).
* `is_active DEFAULT TRUE` is a defensive DB-level match to the model
  default; the repository always supplies the value explicitly and does
  not rely on the default.
* The primary key is a database-level secondary defense; the repository
  detects duplicates explicitly to produce the approved typed 409 path
  rather than surfacing a raw driver error.

## 3. Schema versioning and migrations (Section B)

**Step 1.5b status: the offline runner and the first migration file are
implemented** (`src/db_migrate.py`, `migrations/V001__employee.sql`),
with the contract below verified against fake executors and temporary
directories only. **No database has been touched**: there is still no
version table anywhere, no executor bound to `src/db.py`, and no
migration ever executed against PostgreSQL. At `bb95559`
(`grep -rn "migration|schema version|alembic" src/ tools/` finds no
Phase 3 migration code), so the paragraphs below were the planned
contract when written and are now the implemented one except where a
note says otherwise.

### 3.1 Version tracking

* Ordered SQL scripts in `migrations/` (`V001__employee.sql`, …). The
  version number is the monotonically increasing integer in the file
  name (one migration per file, matching the repository's "one step at
  a time" philosophy).* The current schema version is recorded **inside the database** in a
dedicated table. (Step 1.5b, per the approved OQ-2: one row **per
applied migration**, not a minimal single-row table — this enables
automated checksum drift detection:)

```sql
CREATE TABLE schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    checksum   TEXT NOT NULL,         -- sha256 of the applied file's content
    applied_at TIMESTAMPTZ NOT NULL
);
```* The runner materializes the ledger itself (`ensure_ledger`,
static `CREATE TABLE IF NOT EXISTS`, per the approved 1.5b core); a
versioned migration file never creates the ledger, so the migration
system's own bookkeeping cannot race or double-apply against a
versioned migration's content.
* The runner reads `schema_version` first. A database with **no
`schema_version` row and no `employee` table** is treated as version 0
(prospective empty target). A database containing `employee` but no
matching version row is an **integrity error** — reported and refused,
never silently repaired or re-derived. [REPO, Step 1.5b:
implemented and offline-tested]
* Version N is applied only when the recorded version is exactly N−1
  (no gaps, no out-of-order or missing migrations; applied scripts are
  not re-runnable).

### 3.2 Ordering and application

* Migrations apply in ascending order; **each migration runs inside its
  own transaction**, and the new `schema_version` row is written in that
  same transaction, so the recorded version and the applied DDL commit
  atomically — a half-applied schema with a stale version row is
  structurally impossible.
* The runner is the only permitted writer of schema DDL. The API never
  runs a migration: at startup the G2 gate (§3.8) verifies **read-only**
  that the ledger exactly matches the shipped files, taking the same
  advisory lock, and an incompatible schema is refused before Uvicorn
  starts (implemented in Step 38). Startup schema verification is
  serialized against migration execution through that lock, and the lock
  is released before the API begins serving — so this guarantees that
  **startup verification cannot overlap a migration run**; it does not
  prevent a migration run from starting while an already-running API is
  serving.

### 3.3 Partial-failure handling

* A failure mid-script rolls that migration's transaction back entirely;
  the database remains at its previous version, unmodified by the failed
  script. Nothing is marked "partially applied".
* A failed migration aborts that **operator-run** migration pass with a
  sanitized message; the API never executes migrations, so startup is
  never where a script runs (§3.8). A database left at its previous
  version is then refused at API startup with the G2 diagnostic for the
  mismatch — never a raw driver message, never a DSN. Trusted detail
  stays server-side in the log via the existing `src.logging_setup`
  contract, following the Gate 1.2 redaction discipline and the same
  "no traceback to the console" pattern the runner already uses for
  configuration (exit 2) and guard (exit 3) failures.
* Retrying a failed migration is an operator decision, never automatic
  (same no-implicit-retry rule as `src/db.py` connections and
  `PERSISTENCE_UNCONFIRMED` writes).

### 3.4 Migration status reporting (no credential leakage)

Migration status output shows only: target verified?, current version,
applied version list, and **sanitized error classes** — never a driver
exception string, never the DSN. Wherever a DSN could reach output, the
existing `src/db.py` `_redact()` machinery is reused (D4).

**Step 1.5b-6 status: implemented; the read path is live-verified and
strictly read-only.**

* **Snapshot semantics.** `PsycopgMigrationExecutor.read_snapshot()`
  opens one connection and issues `BEGIN ISOLATION LEVEL REPEATABLE READ
  READ ONLY`, reads ledger presence (`to_regclass`), the public
  table/partition list, and — only when that same transaction reported
  the ledger present — its `(version, checksum)` rows, then `ROLLBACK`s
  and closes. Tables, ledger presence and ledger rows therefore come from
  **one** MVCC snapshot: `status` cannot report a table list from before
  a runner's commit and a version list from after it. It never takes the
  advisory lock, never creates the ledger, and never commits.
* **Derivation is pure.** `read_state(snapshot, migrations_dir=None)`
  performs no database I/O and no writes: it reads the migration files
  through `load_migrations()` and applies `_reconcile()`, so checksum
  drift, duplicate ledger rows, gaps,
  applied-but-missing scripts, and `employee`-without-ledger are judged
  by the exact rules the runner uses, then reports `previous_version`,
  `applied` and `pending`. It refuses an inconsistent snapshot (rows but
  no ledger) instead of deriving a version from contradictory evidence.
* **CLI.** `tools/run_migrations.py status` uses only those two; `apply`
  runs the unchanged `migrate()` over the real executor. The target is
  the configured `ATTENDANCE_DATABASE_URL` only (no default, no
  fallback), printed as `dbname=… host=… port=…`. It refuses a target
  whose normalized identity equals the configured test DSN, and fails
  closed (exit `2`) when a configured test DSN cannot be normalized —
  there is no override flag.
* **Exit codes:** `0` success; `1` unexpected internal error; `2`
  configuration/target refusal; `3` advisory lock held by another run;
  `4` database unavailable/driver failure; `5` migration refused or
  failed (file, checksum, integrity, gap, missing script, application);
  `6` the run completed but lock cleanup failed (outcome unconfirmed).
* **Not claimed:** `status` reports state, not correctness — a ledger
  whose rows match the files says nothing about the server-side schema
  beyond the checks the runner already performs.

### 3.5 Rollback contract

**Operational rollback is non-destructive** (D5):

* Migrations are forward-only; there are **no auto-generated
  down-migrations**.
* Recovery from an undesirable migration is executed from a **verified
  full-database backup** taken before the migration window (a documented
  runbook prerequisite), with restore verification (row counts /
  checksums) before the server resumes.
* Failed mid-script migrations already self-restore through
  PostgreSQL's transactional rollback (3.3) — no operator action.

**Destructive operations (DROP TABLE, TRUNCATE, DROP DATABASE) are
prohibited** unless every condition holds simultaneously:

1. the target is a dedicated, disposable **test** database confirmed by
   the Section 5 positive-verification contract (explicit
   `ATTENDANCE_TEST_DATABASE_URL`, actual connection identity, and the
   `phase3_test_guard` marker row — see 5.2);
2. the operator has explicitly authorized that destructive command for
   that positively verified target;
3. the confirmed database contains nothing but test state (no journal,
   no biometric/embedding artifacts — impossible by design, since D1
   keeps those out of PostgreSQL entirely, and a Tier-2 test database is
   owner-provisioned from empty).

Name patterns, substrings, or markers alone are **never** sufficient —
the whole 5.2 chain must pass.

### 3.6 Migration details not already approved — flagged for owner review

The following specifics were **not** part of the approved conversation
decisions and must be decided before the implementation gate rather than
silently chosen:

* **[OQ-1 — RESOLVED: Option B approved by the owner (audit finding
  H-3)]** The approved *policy* is unchanged: one fixed, documented
  advisory-lock key acquired before any state check, with a typed
  fail-fast refusal (never inference, never waiting, never retrying)
  and nothing leaked by a crashed runner. The *mechanism* is now
  **Option B**: a session-scoped, nonblocking `pg_try_advisory_lock` on
  a **dedicated connection the executor owns and keeps open for the
  whole run**, acquired before the ledger is created or inspected and
  held across every per-migration COMMIT; the runner releases it — and
  the executor closes that connection — from a single `finally` path
  covering success, typed failures, unexpected exceptions, and a failed
  acquisition. A release failure never masks the original error (it
  becomes a note on it, or a typed `MigrationCleanupError` when nothing
  else failed).

  *History, preserved:* the mechanism first recorded here was
  transaction-scoped `pg_try_advisory_xact_lock`. The independent audit
  showed that such a lock is released when the transaction that took it
  ends — before the per-migration transactions it is meant to protect.
  **Option A** (one outer transaction) was considered and rejected by
  the owner: it would have collapsed the approved per-migration commits
  and ledger rows into one all-or-nothing run. **Only the lock mechanism
  changed**; the per-migration transaction and ledger contract is
  untouched.

  If the dedicated lock connection itself dies mid-run, PostgreSQL
  releases the session lock and the run aborts — there is no
  re-acquire, no retry and no recovery attempt. Implemented and
  offline-tested in `src/db_migrate.py`, and the real executor
  (`PsycopgMigrationExecutor`, Step 1.5b-6) takes and releases this lock
  on a dedicated session connection. Live verification is read/lock-only
  against the dedicated test database (contention and release observed);
  a committed migration has never been executed. See
  DEVELOPMENT_STATUS.md Steps 31 and 37.
* **[OQ-2 — RESOLVED, owner-approved]** Version-ledger shape: one row
  per applied migration with checksum (Section 3.1 above), keeping
  automated drift detection; the previously proposed minimal single-row
  default was superseded.
* **[OQ-3 — RESOLVED, owner-approved]** Rollback medium: verified
  full-database backup restore; forward-only scripts, no
  down-migrations; the verified-backup prerequisite belongs to the
  operational runbook (owner-side), never to automated gates.
* **[OQ-4 — RESOLVED, owner-approved]** Migration storage: numbered
  plain-SQL files in `migrations/`, static text only (no parameter
  substitution, no branching), each file run as one
  executor-managed transaction; `CREATE DATABASE` and
  `CREATE INDEX CONCURRENTLY` are prohibited (non-transactional, would
  break the commit-atomicity guarantee). Runtime statement execution
  goes through the re-used `src/db.py` connection layer: the executor
  opens connections with an **explicit** DSN (never the application
  default) and runs each file with a parameterless simple query, so the
  file's statements share its transaction. Implemented in Step 1.5b-6;
  no migration has been executed against an application database.

### 3.7 Known migration-executor gaps (owner-approved, out of scope)

Recorded so the guarantees above are not read as broader than they are.
None of these is repaired in Step 1.5b-6.

* **G2 — implemented in Step 38; see §3.8.** The API now refuses to start
  unless the ledger matches the shipped files exactly. The gate is
  offline-tested only; live verification against an application database
  has not been run (§11.8).
* **G3 — migration files are validated, not hardened.** The loader
  enforces naming, ordering, and checksums; it does not statically
  analyse or sandbox SQL content (no statement allow-list, no content
  provenance). A reviewed script runs with the runner's privileges by
  design (approved as out of scope in the same audit).
* **G7 — cleanup non-masking is scoped to `Exception`.** When the
  executor handles an ordinary `Exception`, cleanup never replaces it: a
  failed rollback is swallowed while the error propagates, and a failed
  close becomes a note on it. A failed close with nothing else in flight
  is itself a typed `MigrationDriverError`. The guarantee does **not**
  extend to a `BaseException` that is not an `Exception` (e.g.
  `SystemExit`): those handlers do not catch it, so no cleanup failure is
  recorded against it — and if the final close then also fails, that
  `MigrationDriverError` replaces the propagating `BaseException`.
  `acquire_advisory_lock` likewise closes its connection only on
  `Exception`, and the pre-existing `migrate()` cleanup path catches only
  `Exception` from `release_advisory_lock()`.

### 3.8 Startup schema compatibility gate (G2, Step 38)

**Implemented and offline-tested; not yet run against an application
database.** `src/startup_schema.py` is the gate; `src/api.py::run_server`
calls it *after* acquiring the single-process guard and *before* Uvicorn is
imported.

* **Strict refusal.** No warning-only mode and no environment bypass: a
  refusal raises the typed `StartupSchemaError`, `tools/run_api.py` prints
  one sanitized line naming the stable code and returns exit `4`.
  Importing the API and `create_app()` stay database-side-effect-free;
  only `run_server()` verifies. `--print-bind` skips the gate entirely.
* **Read-only, one connection.**
  `PsycopgMigrationExecutor.verify_schema_locked()` performs the whole
  lifecycle on **one** physical connection: `pg_try_advisory_lock`
  (nonblocking, no retry) → `BEGIN ISOLATION LEVEL REPEATABLE READ READ
  ONLY` → ledger presence, ledger-shape validation, public table list,
  `(version, checksum)` rows → `ROLLBACK` → state evaluation while the
  lock is still held → `pg_advisory_unlock` whose boolean **must** be
  `True` → close. A `False` from either advisory-lock call is a refusal;
  cleanup never masks a refusal already in flight (G7 scope: `Exception`).
  The six-method ABC and `read_snapshot()`'s ownership contract are
  unchanged; the snapshot SQL is shared through one private helper.
* **Configuration.** `ATTENDANCE_DATABASE_URL` and
  `ATTENDANCE_TEST_DATABASE_URL` are both mandatory for startup. Only the
  application DSN is ever opened; the test DSN is normalized
  (`src/db_target.py`) and compared only, so an application target equal to
  the test target is refused. Missing/blank, malformed, and unidentifiable
  targets are all refused before any connection.
* **Migration rules.** The gate reuses `read_state()`/`_reconcile()` and
  adds only what an exactly-up-to-date database requires: a non-empty
  shipped set, a present ledger, a set beginning at V001, and no pending
  versions. Checksums, unknown versions, duplicates, non-contiguous
  history, malformed files, and `employee`-without-ledger keep their
  existing rules. `migrate()` and `ensure_ledger()` are never called, so a
  missing ledger is refused, never created or repaired.
* **Out of scope.** Physical drift (tables, columns, indexes, constraints,
  triggers, functions) is not inspected; the only physical fact involved is
  the pre-existing `employee`-without-ledger integrity rule. G8 (§3.9) is the
  later stage for exactly that gap; it is now enforced at startup alongside
  this gate.
* **Diagnostic codes** (stable machine surface, fixed sanitized messages):
  `DATABASE_CONFIGURATION_INVALID`, `DATABASE_TARGET_INVALID`,
  `TEST_DATABASE_TARGET_REJECTED`, `DATABASE_UNAVAILABLE`,
  `MIGRATION_LOCK_UNAVAILABLE`, `MIGRATION_LOCK_RELEASE_FAILED`,
  `MIGRATION_SET_EMPTY`, `MIGRATION_LEDGER_MISSING`,
  `MIGRATIONS_PENDING`, `MIGRATION_CHECKSUM_MISMATCH`,
  `UNKNOWN_MIGRATION`, `MIGRATION_HISTORY_INVALID`,
  `SCHEMA_VERIFICATION_FAILED`.

### 3.9 Physical schema compatibility verification (G8, Step 39)

**S1–S5 implemented (the declarative contract, the read-only catalog probe, the
pure evaluator, the executor integration and the startup gate). G8 is in force:
API startup proceeds only when the combined verification returns
`COMPATIBLE`.** G2 (§3.8) verifies the migration *history*; G8 verifies the
*physical* schema the application requires, inside the same one-connection,
one-snapshot read-only operation.

* **Approved architecture (owner sign-off).** Stage name **G8** — the `G3`
  entry in §3.7 is a different, owner-approved gap (migration-file hardening)
  and is deliberately unchanged. One PostgreSQL connection and one consistent
  read-only snapshot shared with G2; schema expectations come from an
  **explicit declarative contract** rather than SQL parsing; unrelated
  objects in `public` are allowed; prohibited objects are limited to
  documented name patterns plus the exact test-only marker; startup
  enforcement is mandatory and fail-closed; a refusal reuses G2's channel
  (one sanitized line, exit `4`), so no new exit code is introduced.
* **S1 — `src/schema_contract.py` (this step).** An immutable contract for
  the two required tables: `employee` (`employee_id text NOT NULL`,
  `full_name text NOT NULL`, `is_active boolean NOT NULL DEFAULT TRUE`,
  `created_at timestamptz NOT NULL` with **no** default — approved D2 — and
  primary key `(employee_id)`) and `schema_version` (`version integer`,
  `checksum text`, `applied_at timestamptz`, all `NOT NULL`, primary key
  `(version)`). Types use canonical `format_type(...)` spellings (`text`,
  `boolean`, `integer`, `timestamp with time zone`), not
  `information_schema` aliases. No secondary index is required: the
  primary-key index already serves the repository's keyset query. Column
  ordinal order is deliberately **not** part of the contract because every
  repository statement names its columns.
* **Prohibited-object policy.** Token-bounded, case-insensitive patterns over
  the unqualified relation name for attendance, embedding, biometric, face and
  enrollment objects (so `surface_metrics` is *not* a face table, while
  `scores_attendance` is), plus the exact `_phase3_test_guard` marker, whose
  presence means the configured target is a test database rather than that the
  schema drifted. Unrelated application tables are explicitly allowed.
* **Pinned to the migration set.** `MIGRATION_SET_DIGEST` is a hard-coded
  SHA-256 over the shipped scripts' `(version, checksum)` pairs — the
  checksums are the existing loader's canonical-text digests, so there is no
  second checksum implementation — and `CONTRACT_MIGRATION_VERSION` names the
  highest version the contract describes. Adding, removing, editing or
  renaming a migration changes the digest and fails
  `tests/test_schema_contract.py` offline until a human re-pins the contract;
  the exact procedure is documented in the module docstring.
* **S2 — `src/db_schema_catalog.py` (this step).** The read-only probe: four
  fixed, schema-qualified `pg_catalog` `SELECT`s (relations of every relevant
  kind, real-table columns, table constraints, table indexes) returning frozen
  `RelationFact`/`ColumnFact`/`ConstraintFact`/`IndexFact` records in a
  `CatalogSnapshot`. It accepts an **existing** connection and never opens,
  closes, commits, rolls back or locks, so it runs inside G2's
  `REPEATABLE READ READ ONLY` snapshot and every fact comes from one point in
  time. Columns use `format_type` with `attnotnull`, `pg_get_expr` (NULL means
  *no* default, so a missing default is never mistaken for an empty one) and
  `attgenerated`/`attidentity` (a generated column has an expression yet
  accepts no inserted value, so it must not read as "defaulted"), plus the
  ordered `attnum`. Constraint columns expand with `WITH ORDINALITY`, so the
  constraint's own column order is preserved and never inferred from names;
  index columns are read positionally over `indnatts`/`indkey` with expression
  positions skipped; definitions come from `pg_get_constraintdef`/
  `pg_get_indexdef`, so no SQL is parsed anywhere. Every statement carries an
  `ORDER BY` **and** is re-sorted in Python, so results never depend on
  planner or driver row order.
* **Why `pg_catalog` and not `information_schema`.** `information_schema` is
  privilege-filtered: it reports only columns the current role holds a
  privilege on, so a least-privilege runtime role would see fewer columns than
  exist and S3 would refuse a correct database. The probe reads only
  world-readable `pg_catalog` relations instead.
* **Failure and scope.** Any read failure raises `CatalogProbeError` with one
  fixed, value-free message (`__cause__` preserved for trusted diagnostics); no
  partial snapshot is ever returned, so a failure cannot look like a smaller
  valid schema. The module holds no policy, defines no diagnostic code, opens
  no connection, imports no test support, and deliberately does not read
  triggers, rules, policies, functions, types, extensions, ACLs or storage
  parameters — none of them expresses a G8 requirement. It reads
  `attgenerated`/`attidentity`, which makes PostgreSQL 12+ a prerequisite that
  the separately authorized live verification must confirm (the repository
  pins no server version).
* **S3 — `src/physical_schema.py` (this step).** The pure evaluator: it compares
  a `CatalogSnapshot` with the S1 contract and returns a frozen
  `SchemaEvaluation` (a `CompatibilityStatus` plus an ordered tuple of
  `Finding`s) with no connection, SQL, file or environment access, no logging
  and no mutable state. Ordinary incompatibility is **data, not an exception**;
  only an unusable call (a non-snapshot value, or an empty, duplicated or
  self-contradictory contract) raises `PhysicalSchemaInputError`.
* **Three outcomes, not two.** `COMPATIBLE` means the findings tuple is empty;
  `INCOMPATIBLE` means at least one fact is definitely wrong; `INDETERMINATE`
  means nothing is definitely wrong but the facts — or the contract — do not
  answer the question. A gate must refuse both, but an operator can tell drift
  from missing metadata. An empty snapshot is therefore never compatible: it
  yields two `SCHEMA_TABLE_MISSING` findings, never a vacuous pass.
* **Diagnostic codes (final S3 vocabulary).** `SCHEMA_OBJECT_PROHIBITED` (the
  S1 rule, applied to every relation kind; the exact `_phase3_test_guard`
  marker carries the distinct `category` `test_marker` so integration reports a
  wrong *target* rather than drift), `SCHEMA_TABLE_MISSING`,
  `SCHEMA_TABLE_KIND_MISMATCH`, `SCHEMA_TABLE_KIND_UNCONFIRMED`,
  `SCHEMA_COLUMN_MISSING`, `SCHEMA_COLUMN_UNEXPECTED`,
  `SCHEMA_COLUMN_TYPE_MISMATCH`, `SCHEMA_COLUMN_NULLABILITY_MISMATCH`,
  `SCHEMA_COLUMN_DEFAULT_MISMATCH`, `SCHEMA_COLUMN_GENERATION_MISMATCH`,
  `SCHEMA_COLUMN_IDENTITY_MISMATCH`, `SCHEMA_PRIMARY_KEY_MISSING`,
  `SCHEMA_PRIMARY_KEY_MISMATCH` and `SCHEMA_METADATA_INCOMPLETE`.
  `SCHEMA_CATALOG_UNREADABLE` is emitted by neither S3 nor S4 (a catalog-read
  failure is S2's `CatalogProbeError`); S5 owns it, as described below.
* **Precedence.** Findings are ordered by that code sequence, then by the S1
  rule category (so the test marker leads), then schema, object, column, code
  and detail — a total order independent of snapshot order, verified by feeding
  shuffled snapshots. A missing or wrongly-typed table makes its columns and
  key unknowable, so only the table finding is reported; a present table whose
  facts are absent, duplicated or contradictory is
  `SCHEMA_METADATA_INCOMPLETE`, never a match.
* **Conservative comparison.** Types are compared case- and
  whitespace-insensitively against the canonical `format_type` spelling only —
  aliases such as `timestamptz` are refused, so no second alias table can
  equate two different spellings. A required default is accepted only when the
  normalized expression is exactly `true`; an expected-`NONE` column must have
  no default; and a generated or identity column is reported as such
  **instead of** as a default, because both put an expression in `pg_attrdef`
  yet `INSERT` cannot write them. Findings never echo a default expression,
  constraint or index definition, SQL text, DSN or driver message — identifiers
  and contract type spellings only.
* **Deliberate exclusions.** Secondary indexes are not evaluated (S1 declares
  none, and S2 cannot separate `INCLUDE` columns from key columns, so a
  contract declaring one is rejected as an unusable call rather than silently
  ignored); no PostgreSQL version is checked (that needs a connection — S4/S5);
  and unrelated public tables, views, indexes, sequences and additional
  constraints produce no finding, so `public` is never turned into an
  allowlist.
* **Owner decision — partitioned tables (final).** A contracted relation with
  `relkind = 'p'` is **not** compatible and **not** definitely incompatible: it
  yields `SCHEMA_TABLE_KIND_UNCONFIRMED` and the result stays `INDETERMINATE`,
  which a gate must refuse. S1 describes the ordinary table `V001` creates; the
  owner confirmed the indeterminate classification stays as-is, so no stage may
  silently convert it in either direction.
* **Owner decision — prohibited-name scope (final).** The S1 prohibited-name
  rules apply to **every relation type S2 returns** — ordinary and partitioned
  tables, views, materialized views, sequences, foreign tables, composite types
  and indexes — not only ordinary tables, so a prohibited view or sequence
  cannot slip through as allowed. The exact `_phase3_test_guard` marker keeps
  its distinct `test_marker` classification.
* **S4 — `src/db_migrate_executor.py` (this step).** The executor integration.
  `PsycopgMigrationExecutor.verify_schema_locked()` gained two keyword-only
  parameters: `catalog_probe` (default `None`; production passes
  `src.db_schema_catalog.read_public_catalog`) and `physical_evaluator` (default
  the S3 evaluator, an offline-test seam). **With no probe the method is the G2
  operation it always was** and returns the bare `MigrationSnapshot`, so every
  existing caller and test is unaffected; with a probe it returns a frozen
  `LockedSchemaVerification(migration, physical)` and follows one fixed order:
  one connection, one ``pg_try_advisory_lock`` attempt, **one**
  `REPEATABLE READ READ ONLY` transaction, the G2 reads, `evaluate(snapshot)`,
  the catalog probe **on that same connection inside that same transaction**,
  the pure S3 evaluation, `ROLLBACK`, the confirmed `pg_advisory_unlock`, and
  the close.
* **G2 precedence and the snapshot guarantee.** The catalog is probed only after
  `evaluate(snapshot)` has accepted the migration history, so every G2 refusal
  (configuration, target, availability, contention, missing or malformed
  ledger, pending, checksum, unknown version, invalid history) is raised before
  a single catalog statement runs and can never be replaced by a physical
  finding. Because the probe runs inside the transaction that read the ledger,
  history and physical facts come from **one consistent snapshot**, and the
  advisory lock covers both reads and both evaluations.
* **Result exposure, not enforcement.** S4 returns the S3 evaluation unchanged:
  `COMPATIBLE`, `INCOMPATIBLE` and `INDETERMINATE` all survive, findings are
  never dropped, and no G2 diagnostic code is added or altered. A catalog read
  failure is re-raised as a fixed-message `MigrationDriverError` with the cause
  preserved — no partial snapshot, no raw driver text. Turning that result into
  a startup refusal is S5's job (below); S4 itself enforces nothing.
* **Cleanup unchanged.** Exactly one connection attempt, one lock attempt, no
  retry; an unconfirmed or erroring `pg_advisory_unlock`, or a close failure, is
  a `MigrationCleanupError` and never a success; a failure already in flight is
  preserved with the cleanup failure attached as a note (G7 scope: `Exception`).
  A failed `ROLLBACK` is a `MigrationDriverError` (*the schema verification
  transaction could not be ended*) and is also never a success.
* **S5 — startup gate (`src/startup_schema.py`, `src/api.py`,
  `tools/run_api.py`) (this step).** `verify_startup_schema()` now calls the
  existing S4 combined operation exactly once —
  `verify_schema_locked(evaluate, catalog_probe=read_public_catalog,
  physical_evaluator=evaluate_physical_schema)` — and then enforces its result.
  The startup layer supplies the production probe and evaluator to the existing
  seam; it never calls S2 or S3 itself, never opens a second connection, never
  takes a second lock and never starts a second transaction. There is one gate
  and no weaker second entry point.
* **Strict fail-closed decision rule.** Startup is permitted only by a
  `LockedSchemaVerification` whose physical status is `COMPATIBLE`. Every other
  outcome refuses: `INCOMPATIBLE` → `SCHEMA_INCOMPATIBLE`, `INDETERMINATE` →
  `SCHEMA_INDETERMINATE` (partitioned tables therefore refuse startup while
  still being reported as unconfirmed rather than as drift), a catalog-read
  failure → `SCHEMA_CATALOG_UNREADABLE`, an unexpected result object or an
  evaluator exception → `SCHEMA_VERIFICATION_FAILED`, and a cleanup failure
  (rollback, unlock or close) keeps its existing `MIGRATION_LOCK_RELEASE_FAILED`
  / `MIGRATION_HISTORY_INVALID` meaning and wins before any physical verdict is
  read, because the refusal is raised only after `verify_schema_locked()` has
  returned. A missing findings tuple, an empty snapshot, an unknown status and
  an exception are never treated as compatibility. A bare `MigrationSnapshot`
  (the no-probe form) is not a successful combined verification, so it refuses
  `SCHEMA_VERIFICATION_FAILED` instead of silently skipping G8.
* **G2 precedence preserved.** Because S4 probes the catalog only after the
  migration history is accepted, every G2 code keeps its existing meaning,
  ordering and message, and a history failure is never replaced by a physical
  finding. On the physical side the S3 codes keep their vocabulary and total
  order: they are carried unchanged in `StartupSchemaError.findings` (a frozen
  tuple of the same frozen `Finding` objects) and exposed as `finding_codes`, so
  `str(error)` is still exactly `"<CODE>: <message>"`.
* **Diagnostic mapping.** Two gate-level codes are added for the two S3
  statuses (`SCHEMA_INCOMPATIBLE`, `SCHEMA_INDETERMINATE`);
  `SCHEMA_CATALOG_UNREADABLE` is reused from the reserved S3 vocabulary and is
  recognised by inspecting the preserved `CatalogProbeError` cause on S4's
  `MigrationDriverError`, so a catalog failure never becomes an empty snapshot
  or an ordinary mismatch and S4 needed no change. `MIGRATION_LOCK_RELEASE_FAILED`
  and `MIGRATION_HISTORY_INVALID` are untouched.
* **Startup sequence and refusal.** The order is unchanged — single-process
  guard, configuration and target validation, combined G2+G8 verification,
  then Uvicorn. A refusal raises the typed `StartupSchemaError` after
  `verify_schema_locked()` has already rolled back, unlocked and closed, so the
  gate releases the single-process guard through the existing established path
  and Uvicorn is never imported or started. `run_api.py` keeps exit code `4`
  (no new exit code per finding) and prints the one existing sanitized refusal
  line followed by at most eight `run-api: schema finding: <CODE>
  <schema.object[.column]>` lines — stable codes and identifiers only, never a
  DSN, credential, SQL text, default expression, driver message or traceback.
  `--print-bind`, `create_app()` and module import remain database-independent
  and side-effect-free, and the six-method executor ABC is unchanged.
* **Pending.** S6 (audit/documentation completion), S7 (owner-run negative
  database provisioning) and S8 (separately authorized live read-only
  verification) remain. G8 is enforced at startup and offline-verified only:
  the probe and the gate have never been executed against a PostgreSQL server,
  and PostgreSQL 12+ remains an unverified prerequisite.

## 4. Employee repository interface (Section C)

**Step 1.5b status: implemented offline** in
`src/employee_repository.py`, tested against injected fakes
(`tests/test_employee_repository.py`). No database has been connected
to, so the persistence behavior is contract-verified only. The
production connection source is now bound by the Step 1.5c-3a wiring
(Section 11), also offline-tested only. The remaining unimplemented
parts of this design are the API endpoints that would consume the
repository (a later gate).

The contract below is unchanged by implementation; it is aligned with
the existing model and API decisions (D2, D7). The repository injects a
connection source at construction; it holds no module-level connection
and imports no driver at module level (mirroring the `src/db.py` hygiene
contract, already test-enforced).

```
EmployeeRepository(connection_factory, ...)
```

Note: the repository must also **never connect when the configured DSN
is blank** — the existing `src/db.py` rule that a blank URL means a
`ValueError`-shaped configuration failure (never psycopg's local-default
connection behavior) applies through this layer unchanged.

### 4.1 Operations

* `create(employee: Employee) -> Employee`
  * Inserts `employee_id`, `full_name`, `is_active`, `created_at` from
    the model's already-validated values (including the
    application-authoritative `created_at`, D2). Returns the same
    `Employee`.
  * **Duplicate `employee_id`:** detected explicitly (not via a raw
    driver error) → typed `EmployeeAlreadyExists` (API 409, D7). No
    retry, no alternate-ID generation.
  * **Invalid input** (blank ID, blank name, naive datetime, wrong
    types): the existing model's `EmployeeValidationError` is raised
    **before any DB call**; the repository never coerces or sanitizes.
  * **Database unreachable:** the existing `DatabaseUnavailableError`
    propagates untouched — the repository introduces no catch-all that
    would blur Gate 1.2's availability/config distinction.
* `get(employee_id: str) -> Employee | None`
  * Returns the `Employee`, or `None` when absent (the caller maps that
    to the approved 404). Non-empty ID validated; no wildcard/`LIKE`
    semantics; exact match only.
* `list(...) -> list[Employee]`
  * **Bounded pagination** with the approved constants **[OQ-6 —
    RESOLVED, owner-approved]**: page size 20, hard maximum 100, as
    frozen `PaginationConfig` constants in `src/config.py` (the
    earlier draft of 25 was explicitly superseded). Values above the
    maximum are clamped to it; non-integer/boolean/sub-1 values are
    refused before any DB call. Attendance behavior is untouched —
    no attendance pagination exists.
  * Ordering is the stable, documented, index-backed key
    (`employee_id` ascending), **keyset-only** (`WHERE employee_id >
    last_seen_id`); there is no plain-`OFFSET` path and no
    consistent-snapshot claim across separate requests: each request
    has its own consistency boundary, and rows inserted between
    requests can affect subsequent pages. [REPO, Step 1.5b:
    implemented and offline-tested]
* `update_full_name(employee_id: str, full_name: str) -> Employee`
  * `full_name` is the **only** mutable field in v1 (approved API rule,
    D7). Blank/whitespace-only value → `EmployeeValidationError`, made
    **before any DB call**. Missing employee → typed
    `EmployeeNotFound` (API 404).
* `deactivate(employee_id: str) -> Employee`
  * Sets `is_active = FALSE`, returns the resulting employee. Missing
    employee → `EmployeeNotFound`. Inactive employees cannot check out —
    this remains enforced downstream by the engine
    (`INACTIVE_EMPLOYEE`) and, later, the API layer; the repository only
    persists status faithfully. Residual open sessions of a deactivated
    employee belong to the authoritative journal's reconciliation
    process — **never** auto-resolved here (D1/D7).

### 4.2 Error mapping (proposed; machine codes already reserved)

| Condition | Repository error | API mapping (v1, approved) |
|---|---|---|
| duplicate ID | `EmployeeAlreadyExists` (new, typed) | 409 |
| missing ID | `EmployeeNotFound` (new, typed) | 404 |
| invalid field value | existing `EmployeeValidationError` | 422 |
| PostgreSQL unreachable | existing `DatabaseUnavailableError` | 503 `DATABASE_UNAVAILABLE` (already reserved in `src/api.py`, unregistered routes only) |
| blank/malformed DSN | existing `ValueError` family / `MalformedDsnError` | 503 `DATABASE_CONFIG_INVALID` (already reserved) |
| unexpected driver error | surfaced only after the Gate 1.2 redaction boundary sanitizes it | 500 `INTERNAL_ERROR` (existing sanitized envelope) |

The repository intentionally reuses `src.db`'s existing two-family
taxonomy (value-contract vs availability) rather than adding a third.

## 5. PostgreSQL integration-test safety contract (Section D)

Two strictly separate test tiers; Tier 2 runs **only** with explicit
separate authorization and only after full positive target verification.

### 5.1 Tier 1 — offline tests (no PostgreSQL, always allowed)

* Repository and migration-runner logic tested against **injected
  fakes** implementing only the real interface (same convention as the
  existing core suites: temporary directories, synthetic state,
  hardware-free).
* Coverage: duplicate-ID path; missing-ID path; pagination windows
  including last page and clamping; `full_name` update semantics;
  deactivate semantics; invalid inputs raising before any DB call; a
  fake "unavailable" connection surfacing as
  `DatabaseUnavailableError`; version parsing, ordering, and gap
  detection in the runner; **no initialize/connect is ever attempted**
  (asserted by the same fresh-subprocess import-hygiene pattern
  `tests/test_api.py` and `tests/test_db.py` already use).

### 5.2 Tier 2 — positive-verification contract (before any destructive setup)

**Step 1.5b status: the guard that implements this chain is implemented
and offline-tested** (`src/test_database_guard.py`,
`tests/test_test_database_guard.py`), and read-only mode is now
**enforced on the connection** (`connection.read_only = True`, verified
before the first query) rather than merely being a property of the
statements the guard happens to issue. The guard opens one connection
and always closes it, on success and on every blocked path, without
committing. **Live status:** this chain has since run against the
owner-provisioned dedicated test database only — `READY` in Step 1.5c-3b
(marker and ledger both at version 1) — and the migration executor's
read/lock paths were verified against that same target in Step 1.5b-6.
No application database has ever been contacted.

An integration test may touch the database only after **all** of the
following pass, in order; any failure or ambiguity → typed **BLOCKED**,
no write, no teardown:

1. `ATTENDANCE_TEST_DATABASE_URL` is **explicitly set** in the test
   process. Blank or missing → BLOCKED. Never falls back to psycopg's
   local-default connection behavior (existing `src/db.py` rule).
2. The test **connects through that exact configured DSN** and verifies
   the actual connected database identity from the connection itself
   (`current_database()`, plus the connection's host/port as psycopg
   reports them from the established connection — not reconstructed
   from a string). A configured-vs-actual mismatch → BLOCKED.
3. The **`phase3_test_guard` marker table exists in the connected
   database and contains the expected marker row** (the marker is a
   server-defined truth inside the target, never a client-side
   assertion). Missing/mismatched/ambiguous → BLOCKED. A plausible
   database *name* (substring, prefix, naming convention) alone is
   **never** sufficient.
4. The connected target is not the application's target: the test
   asserts its connection did not use `ATTENDANCE_DATABASE_URL` (a
   deliberate tripwire, mirroring the Step 1.3 test
   `src.db._connect`-never-called pattern, and confirms the two
   variables resolved to different databases).
5. Each destructive operation (drop/truncate/seed-reset) is explicitly
   listed in the test's authorized destructive-action manifest;
   anything outside the manifest is **refused, not attempted**.

Only after 1–5 pass may the test perform its destructive setup, and
teardown must satisfy the same manifest. Every Tier-2 suite must
produce, in its output, the positive evidence items above — the fail
path is a typed BLOCKED, not a bare assertion failure that a future
maintainer might weaken.

### 5.3 Marker contract

`phase3_test_guard` exists inside the test database only. **[OQ-5 —
RESOLVED, owner-approved; implemented (offline) in Step 1.5b:**

* table `_phase3_test_guard` with columns
  `marker TEXT NOT NULL UNIQUE` carrying the fixed phase-tagged
  literal `VISUAL_ATTENDANCE_PHASE3_TEST_ONLY`,
  `created_at TIMESTAMPTZ NOT NULL` (tz-aware; naive → ambiguous →
  BLOCKED), and
  `schema_version INTEGER NOT NULL` that must equal the applied
  migration ledger's current version at check time.
* Approved check tightenings: **both** `ATTENDANCE_DATABASE_URL` and
  `ATTENDANCE_TEST_DATABASE_URL` must be configured for the guard to
  run at all; configured targets are compared by normalized identity
  (dbname/host/numeric port; textual host aliases treat as one local
  server; user/credential differences can neither establish nor fake
  distinctness); connected identity is read from the connection object
  itself (`connection.info`, then `current_database()`).
* Identity is **only ever positively established** (audit repair): a
  DSN carrying `hostaddr` or `service` (both reroute the connection), a
  missing host (libpq's local-socket default), or a missing database
  name is ambiguity → `IDENTITY_AMBIGUOUS`, never an assumption that
  the targets differ. An omitted port is not ambiguous: libpq's default
  is 5432. The driver-reported connected host/port are compared with
  the declared test target (`CONNECTED_TARGET_MISMATCH`) and with the
  application target (`TARGETS_NOT_DISTINCT`).
* Read-only is **enforced**, not merely observed: the connection's
  `read_only` attribute is set and read back before the first query;
  if that cannot be established → `READ_ONLY_UNAVAILABLE` with nothing
  read. Marker absence, marker read failure, and multiple marker rows
  are distinct BLOCKED reasons, and every path closes the connection
  without committing.
* The destructive-action manifest is a separate gate
  (`require_destructive_action`) usable only with a
  verification-produced target. `VerifiedTestTarget` cannot be
  constructed directly, and a manifest must be a collection of
  non-blank strings (a bare string is refused so substring membership
  cannot pass). This is a **mistake-guard library contract, not a
  security boundary**: in-process code can reach the module's private
  proof object, and the guard does not claim otherwise.
* The marker is **established by the owner** when provisioning the
  dedicated test database (Section 6, step 1.5b-3) — establishing it is
  itself a database write and therefore outside automated acceptance
  gates. Not yet exercised against a live PostgreSQL server.]

## 6. Implementation sequence and acceptance gates (Section E)

Strictly dependency-ordered; every gate is a **stop-and-report** point;
nothing proceeds to the next row without owner authorization. "Tier 2"
means a PostgreSQL connection is required (Section 5.2 contract).

| # | Step | Files (anticipated, not authorized) | DB? | Gate |
|---|---|---|---|---|
| 1.5a | **(this step)** Planning record + status/env corrections only | `DB_DESIGN.md` (new), `DEVELOPMENT_STATUS.md`, `.env.example` | No | Owner review of the plan |
| 1.5b-1 | Migration SQL + version-tracking + runner skeleton + Tier-1 runner tests | `migrations/V001__employee.sql`, `src/db_migrate.py`, `tests/test_db_migrate.py` | No | **Implemented (offline) in Step 1.5b; runtime wiring pending** |
| 1.5b-2 | Employee repository + Tier-1 fake tests | `src/employee_repository.py`, `tests/test_employee_repository.py` | No | **Implemented (offline) in Step 1.5b** (plus `src/test_database_guard.py` + guard tests, the approved OQ-5 offline core) |
| 1.5b-3 | Owner provisions the dedicated test database and establishes the `phase3_test_guard` marker; `ATTENDANCE_TEST_DATABASE_URL` set locally (never committed) | none (owner-run) | Yes — provisioning is owner-only | Owner confirms target & marker |
| 1.5b-4 | Tier-2 integration tests under the Section 5.2 contract | `tests/test_employee_repository_integration.py` | **Yes** | Explicit owner authorization for *this specific* DB access |
| 1.5b-5 | Regression + docs | `DEVELOPMENT_STATUS.md`, `README.md`, `CHANGELOG.md` | No | Owner review of actual evidence |
| 1.5b-6 | Real psycopg executor, read-only `status` snapshot, migration CLI, Tier-1 + Tier-2 verification | `src/db_migrate_executor.py`, `src/db_target.py`, `src/db_migrate.py` (state types), `tools/run_migrations.py`, `tests/test_db_migrate_executor.py`, `tests/test_db_target.py`, `tests/test_run_migrations_cli.py`, `tests/tier2_support.py`, `tests/test_tier2_migration_executor.py` | **Yes** (read/lock only) | **Implemented and verified in Step 1.5b-6** (Sections 3.4, 3.7, 11.7) |
| 1.5b-7 | G2 startup schema compatibility gate (read-only verification in `run_server`, exit `4`) | `src/startup_schema.py`, `src/db_migrate_executor.py`, `src/api.py`, `tools/run_api.py`, `tests/test_startup_schema.py`, `tests/test_api_startup_gate.py` | **Yes** (read/lock only, **not yet run**) | **Implemented (offline) in Step 38** (Sections 3.8, 11.8); live application-database verification pending separate authorization |
| 1.5c-1 | **Employee-metadata export** from the enrollment manifests (read-only) | `src/employee_export.py`, `tools/export_employee_metadata.py`, `tests/test_employee_export.py` | No | **Implemented (offline) in Step 1.5c-1** |
| 1.5c-2 | Importer over the exported JSONL (fake-backed tests only) | `src/employee_import.py`, `tools/import_employee_metadata.py`, `tests/test_employee_import.py` | No | **Implemented (offline) in Step 1.5c-2** under the approved reconciliation policy (Section 10) |
| 1.5c-3 | Real import into PostgreSQL | `src/db_employee_repository.py`, `tests/test_db_employee_repository.py` (wiring), then none (owner-run) | **Yes** for the run itself | **Wiring implemented (offline) in Step 1.5c-3a** (Section 11); the real import remains owner-gated and additionally requires 1.5b-3 (provisioned DSN + marker) |
| 1.5c+ | DB-backed API endpoints; checkout endpoint | not yet designed | Yes | later gate, separate design |

**Acceptance criteria before any Step 1.5 code milestone is accepted:**

* New modules import side-effect free; no `psycopg` import at module
  load; no blank-DSN fallback (hygiene-test-enforced, extending the
  existing patterns).
* Tier 1 fully green, offline.
* Tier 2 green only under the Section 5.2 contract; an
  unmet/ambiguous condition produces the typed BLOCKED, not a weakened
  assertion.
* Existing suites remain green (last full offline run after Step 1.5b-6:
  **1629 passed, 31 skipped**; the 31 skips are the two opt-in Tier-2
  modules, and 82 of the passing tests are Step 1.5b-6's own Tier-1
  additions).
* Docs updated only with actually observed evidence, per the
  `DEVELOPMENT_STATUS.md` convention ("updated after every verified
  step — never ahead of implementation").

## 7. Out of scope (explicit, per approved decisions)

* Any API endpoint implementation — all seven deferred routes remain
  501 `NOT_IMPLEMENTED` in this gate.
* Authentication / RBAC / CORS changes (Phase 5).
* Camera, embeddings, enrollment, identity/recognition behavior —
  untouched (Phase 1 software regression is green; empirical CV
  evaluation remains BLOCKED, unchanged).
* Attendance-journal or attendance-engine semantics — the JSONL journal
  remains the sole authoritative journal (D1); no PostgreSQL attendance
  tables (D1), no journal reads/writes in this gate.
* Docker/deployment (Phase 6).
* Any Git mutation (staging/commit/push/pull/stash/reset/branch) — the
  repository owner alone commits.
* Multi-machine or multi-process concurrency protection beyond the
  existing single-machine guard (D3); loopback-only binding is a
  development safeguard, not an authentication or network-exposure
  mechanism.

## 8. Open questions requiring owner decision before Step 1.5b

| ID | Question | Proposed default (for review) |
|---|---|---|
| OQ-1 | Concurrent migration-runner policy | **RESOLVED (owner, after the audit's H-3 finding): Option B** — session-scoped `pg_try_advisory_lock` on a dedicated connection held for the whole run, acquired before any state check, released in `finally` with the connection closed; per-migration transactions and the ledger contract unchanged. Supersedes **only** the earlier transaction-scoped *mechanism* recorded here; Option A (one outer transaction) was rejected because it would collapse per-migration commits. |
| OQ-2 | Version-ledger table shape | **RESOLVED:** one ledger row per applied migration with checksum (draft minimal single-row default superseded) |
| OQ-3 | Rollback medium | **RESOLVED:** verified full-database backup restore; forward-only, no down-migrations |
| OQ-4 | Migration storage form | **RESOLVED:** plain SQL files in `migrations/`; static text; one transaction per file; non-transactional DDL prohibited; real execution via `src/db.py` deferred to runtime wiring |
| OQ-5 | `phase3_test_guard` marker details | **RESOLVED:** `_phase3_test_guard(marker TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL, schema_version INTEGER NOT NULL)`, literal `VISUAL_ATTENDANCE_PHASE3_TEST_ONLY`, both DSNs required, normalized identity comparison, read-only-until-verified, typed `GuardBlocked` fail-closed |
| OQ-6 | Pagination defaults | **RESOLVED:** page 20, max 100, frozen `src/config.py` constants; keyset-only ordering; no cross-request snapshot claim **(25 default explicitly superseded by the owner)** |
| OQ-7 | JSONL → PostgreSQL employee-metadata importer | **RESOLVED (deferred):** no importer in 1.5b; Worker Step 1.5c requires a separate owner decision on the actual source format and reconciliation procedure; PostgreSQL becomes the authoritative read source for employee metadata only once DB-backed endpoints exist. **Source settled in Step 1.5c-1 (see Section 9):** the Step 07 manifest `employee` section, exported as a versioned five-key JSONL artifact; the importer itself is still deferred |

## 9. Step 1.5c-1 — employee-metadata export (owner-approved, implemented offline)

**Status: export implemented and offline-tested; nothing imported, no
PostgreSQL connection, no live database behavior verified.** This section
records the owner-approved Step 1.5c source decision and the artifact
contract actually implemented in `src/employee_export.py`,
`tools/export_employee_metadata.py`, and `tests/test_employee_export.py`.

### 9.1 Source of truth for employee metadata (evidence, not invention)

Repository investigation at `60289ff` found exactly one structured
employee-metadata record:

* The Step 07 enrollment manifest
  `<DATA_DIR>/employees/<encoded-id>/employee.json`,
  `format_version: 1` (`MANIFEST_FILENAME` / `MANIFEST_FORMAT_VERSION` in
  `src/enrollment.py`), whose `employee` section is
  `Employee.to_dict()` — the four approved fields and nothing else.
* **No employee-metadata JSONL exists.** The only JSONL is the attendance
  journal (five keys, employee ID only; `src/attendance_store.py`), which
  is not a metadata source.
* Embedding sidecars (`<encoded-id>.meta.json`) carry
  `format_version`/`employee_id`/`dimension`/`dtype`/`stored_at` — no
  name, no status — so they cannot supply metadata.
* Employees are created only by enrollment, so **never-enrolled people
  have no metadata anywhere**, and `tools/delete_enrollment.py` removes
  all artifacts with no tombstone, so **deleted employees are
  indistinguishable from never-enrolled ones**. Both are outside the
  import population.
* Post-enrollment name/status changes are impossible today (nothing
  writes `is_active` after enrollment), so the manifest cannot currently
  be stale relative to the database — but recognition keeps reading the
  manifest's status, so a divergence window exists once DB-backed reads
  arrive. That is recorded, not resolved, here.

Approved: manifests are the **sole** upstream source; the importer never
reads enrollment artifacts.

### 9.2 Artifact contract

`<DATA_DIR>/exports/employee_metadata.jsonl` (override with `--output`),
one JSON object per line, UTF-8, LF, no header:

```json
{"schema_version": 1, "employee_id": "EMP-0042", "full_name": "Ada Lovelace", "is_active": true, "created_at": "2026-10-31T09:12:44.123456+00:00"}
```

* Exactly these five keys; `schema_version` is the integer `1`.
* `employee_id` comes from inside the manifest (the directory name is only
  *checked* against `safe_dir_name`); normalized-away IDs/names are
  rejected, never rewritten.
* `created_at` is the manifest's original enrollment timestamp, preserved
  **verbatim** (approved: it is application-authored at enrollment, so the
  export never re-stamps import/export time).
* Ordering is `employee_id` code point — a stable export order, **not** a
  claim about PostgreSQL collation; the importer must not rely on it.
* The `samples` array, `sample_count`, file paths, images, embeddings, and
  attendance events never appear.
* The artifact contains personal data (names): git-ignored
  (`data/exports/**`), owner-permission-restricted where the platform
  supports it, and never printed by the CLI.

### 9.3 Implemented behavior

* **Read-only scan** confined to the manifest root; symlinked entries,
  symlinked manifests, and a symlinked root are refused, so the scan
  cannot escape the configured directory.
* **Typed rejections** (17 codes) for malformed, unsupported, incomplete,
  or inconsistent manifests; duplicate employee IDs are ambiguous
  identity and every manifest in the group is rejected.
* **Atomic publication**: temporary file → `fsync` → permission step →
  `os.replace`; a failure removes the temporary file and leaves any
  previous artifact untouched.
* **No silent overwrite**: an existing artifact is refused
  (`ExportOutputExistsError`, CLI exit 2) unless overwrite is explicit.
* **Fail-closed default**: any rejection means no artifact at all;
  `--allow-partial` writes only valid records and still reports every
  rejection. A non-empty root with no valid record writes nothing; a
  genuinely empty root writes an empty artifact.
* **CLI exit codes**: `0` success, `1` fail-closed/partial/write failure,
  `2` existing-artifact refusal; output is counts, paths, directory names,
  and reason codes only — never a name.

### 9.4 Still open (not settled by this step)

* The importer policy questions raised here were settled in Step 1.5c-2
  (Section 9.5): create-if-absent, skip-if-identical, report conflicts
  without updating, reject invalid/duplicate records, stop on
  operational errors, and report differing field *names* only.
* The real import (Step 1.5c-3): requires the owner-provisioned dedicated
  test database, its `_phase3_test_guard` marker, and the psycopg-backed
  runtime wiring that does not exist yet. Tier-2 verification remains
  outstanding.
* Reconciliation of the interim divergence between manifest-based
  recognition gating and future database reads (Section 9.1), and the
  treatment of never-enrolled/deleted employees.

## 10. Step 1.5c-2 — employee-metadata importer (implemented offline)

**Status: implemented and offline-tested; no database was contacted and
no real import has run.** `src/employee_import.py` and
`tools/import_employee_metadata.py` consume **only** the Section 9
artifact; they never read enrollment manifests, face samples,
embeddings, or the attendance journal.

### 10.1 Input validation (exactly the Section 9.2 contract)

Every line is validated strictly: JSON object only, all five keys
present and no others, `schema_version` the integer `1`,
`employee_id`/`full_name` valid **and unchanged by the model's
normalization**, `is_active` a real bool, `created_at` a timezone-aware
ISO-8601 string preserved as supplied (never replaced with import time).
Rejections are typed (`malformed_json`, `missing_keys`,
`unexpected_keys`, `unsupported_schema_version`, `invalid_employee_id`,
`id_normalization_changed`, `invalid_full_name`,
`name_normalization_changed`, `invalid_is_active`, `invalid_created_at`,
`invalid_record`, `duplicate_employee_id`) and carry the line number and
(the ID when readable) — never record contents. Duplicate IDs inside one
artifact reject **every** occurrence.

### 10.2 Approved reconciliation policy

| Existing employee state | Behavior |
|---|---|
| Does not exist | Create it (`--apply` only) |
| All fields identical | Skip as identical |
| Differs in any field | Report conflict, list the differing field names, **never update** |
| Invalid input record | Reject that record and continue |
| Duplicate ID in input | Reject every occurrence of that ID |
| `EmployeeAlreadyExists` during create | Report conflict (race); no update, no retry |
| Unexpected repository error | Stop safely: no further reads/writes, report only the exception type name |

The importer uses exactly two repository calls — `get` (classification)
and `create` (approved inserts). There is no name-update path, no
deactivation path, and therefore no way for a conflict to become a write.
`created_at` is never modified.

### 10.3 Modes and exit codes

* `validate-only` (CLI `--validate-only`) — no repository, no database:
  schema/duplicate validation only. It never imports the wiring, so it
  needs no DSN, no driver, and no connectivity.
* `dry-run` — validates **and classifies** against the repository and
  writes nothing. This is the CLI default. Since Step 1.5c-3a the CLI
  builds the production wiring for this mode with **server-enforced
  read-only connections** (Section 11); before that wiring existed it
  refused with exit 2.
* `apply` (CLI `--apply`) — performs the approved creates only, on a
  read-write connection.

Exit codes: `0` clean; `1` rejections, conflicts, a stopped run
(including an unavailable database, whose reason is recorded by
**exception type name** only), or a report-write failure; `2`
CLI/configuration refusal (bad flag combination, missing/unreadable
input, existing report without `--overwrite-report`, or no repository at
all) with nothing written to a database. Reports are JSON containing
counts, employee IDs, differing field names, line numbers, and reason
codes only — no names, timestamps, record contents, DSNs, or driver text.

### 10.4 Not done in Step 1.5c-2 (moved to 1.5c-3)

* The psycopg-backed wiring did not exist in 1.5c-2: that step injected
  the repository and exercised `apply` only against in-memory fakes.
  The wiring itself is now implemented offline in Step 1.5c-3a
  (Section 11).
* No real employee data has been imported; the population limits of
  Section 9.1 (never-enrolled and deleted employees) still apply.
* The interim divergence between manifest-based recognition gating and
  future database reads remains recorded, not resolved.

## 11. Step 1.5c-3a — PostgreSQL repository wiring (implemented offline)

**Status: implemented and tested offline; no PostgreSQL server was
contacted.** This step supplies the production wiring that Step 1.5c-3's
real import needs. It does **not** import employee data, run migrations,
or verify anything against a live database.

### 11.1 Shape

| Concern | Decision |
|---|---|
| Module | `src/db_employee_repository.py` — wiring only: **no SQL**, no second persistence implementation, no retry, no fallback source |
| Bound repository | the approved `EmployeeRepository` (Section 4); statements and typed errors stay there |
| Connection source | `src.db.open_connection` (Step 1.2), imported **lazily** inside the default opener |
| DSN | explicit argument, or `None` → `ATTENDANCE_DATABASE_URL` re-read per attempt by the connection layer (never cached in the wiring) |
| Import hygiene | importing the wiring imports neither `src.db` nor psycopg, opens nothing, reads no DSN |
| Entry point | `open_employee_repository(dsn=None, *, read_only=False, connection_opener=None)` |

`connection_opener` is a documented test seam only: production callers
never pass it, and the production CLI passes `read_only=not apply`.

### 11.2 One operation = one connection

Every repository call acquires a fresh connection through a scope that
owns the complete lifecycle:

1. open (availability failures propagate unchanged as
   `DatabaseUnavailableError`);
2. for `read_only=True`, request **and read back** read-only mode before
   any statement is issued — a connection that cannot be set or does not
   report read-only is refused (fail closed);
3. yield the connection to the repository's single `execute`;
4. `commit()` on success (a read-only transaction commits as a no-op);
5. `rollback()` when the body raises, then re-raise the **same**
   exception object (its type is the importer's stop reason);
6. always `close()`.

Cleanup never replaces the real failure: a rollback error is swallowed
while another exception is propagating, and a close error is attached as
an `add_note` note to that exception. A close error with nothing else in
flight is raised, because a leaked connection is a fault rather than a
footnote. Cursors are driver-owned and are released with the connection;
the wiring does not invent a cursor policy of its own.

### 11.3 Dry run vs apply

* `dry-run` (CLI default): repository built with `read_only=True`, so
  PostgreSQL itself refuses any write the code might attempt. "Only
  SELECTs are sent" is explicitly **not** treated as equivalent.
* `apply`: repository built with `read_only=False`; only the approved
  `create` path writes, and only for absent employees.
* `validate-only`: no repository at all; the wiring module is never
  imported, so no DSN, driver, or connectivity is required.

### 11.4 What is verified offline, and what is not

Offline tests (`tests/test_db_employee_repository.py`, plus the CLI
selection tests in `tests/test_employee_import.py`) run the **real**
repository SQL against a fake psycopg-3-shaped server: statement text and
bound parameters, duplicate detection via `sqlstate == "23505"`,
read-only requested before the first statement, close on every path,
rollback on failure, unchanged error propagation, DSN pass-through, and
module import hygiene. The importer is exercised end-to-end over that
wiring (dry run never writes; apply creates only missing rows; conflicts
and create races never write).

**Not verified, and not claimed:** that PostgreSQL actually refuses the
write in a read-only transaction (the fake cannot model server-side
enforcement), real psycopg connection/commit/close behaviour, DSN
resolution from a real environment, and everything Tier-2 covers
(provisioned database, marker, real import). The fake models the *shape*
the wiring relies on, not driver or server behaviour.

### 11.5 Trust boundary

`read_only` and `connection_opener` are ordinary in-process arguments: the
wiring is a fail-closed mistake guard for callers, not a defence against
malicious code running in the same interpreter. Its guarantees are about
the code paths this project actually writes.

### 11.6 Tier-2 verification harness (prepared, not executed)

`tests/test_tier2_employee_repository.py` exercises everything §11.1–
11.5 describe against the real server; it has run against the
owner-provisioned test database (Step 1.5c-3b: 13 passed, 0 skipped). It is deliberately unable to run uninvited: every item
skips itself at collection unless `ATTENDANCE_TIER2_OPT_IN=1`, and with
the opt-in on it still refuses to start unless both the test DSN and the
application DSN are configured — then the unmodified guard
(`src/test_database_guard.py`) decides, and any blocked outcome skips
with the guard's typed reason. Repositories are built only with an
explicit `dsn=<test DSN>`; the application DSN travels to the guard
alone, for the distinctness proof.

The harness touches the schema in exactly one way: guarded cleanup of
rows it created. Deletion requires a guard-produced `VerifiedTestTarget`,
the single authorized operation in an explicit manifest
(`require_destructive_action`), harness-shaped synthetic IDs only
(`T2-<8-hex-run>-<slot>`), and an exact re-read match of the stored
`(full_name, is_active, created_at)` tuple — one bound-parameter
`DELETE … = ANY(%s)`, never `LIKE`, never DDL, and rows whose ownership
cannot be positively established are left untouched and reported.
Harness-control tests (`tests/test_tier2_harness_control.py`) keep that
boundary offline-tested: pre-flight ordering, explicit test-DSN
targeting, manifest and ownership gating, and an AST audit that the
module's SQL is exactly a bound-parameter SELECT and DELETE.

What this section does **not** cover: server behaviour beyond what the
live runs exercised. Step 1.5c-3b ran the harness against the provisioned
test database (13 passed, 0 skipped), so the §11.4 "not verified" list no
longer applies unchanged to the repository wiring. The marker row and
migration ledger remain owner-established during provisioning (§5.3),
never by any automated step; §11.7 records the executor's own live scope.

### 11.7 Migration executor verification (Step 1.5b-6)

`src/db_migrate_executor.py` implements the six-method runner contract
against real psycopg connections, plus `read_snapshot()` for `status`
(Section 3.4) which the ABC deliberately does not carry. Offline coverage
(`tests/test_db_migrate_executor.py`, `tests/test_db_target.py`,
`tests/test_run_migrations_cli.py`) drives a recording fake connection and
pins: statement text and bound parameters, ledger-presence-before-CREATE
ordering, shape refusal for four divergent ledgers, rollback on a failed
script and on a failed ledger insert, the three cleanup combinations that
actually exist (a failing close with a primary error in flight — note
added, primary preserved; a failing close with nothing else in flight —
typed `MigrationDriverError`; a failing rollback with a primary error in
flight — swallowed, primary preserved), the lock
lifecycle including a refused acquisition that leaves nothing open, the
single read-only snapshot, every CLI exit code, credentials-free output,
and import hygiene (no driver import at module load).

Live Tier-2 coverage (`tests/test_tier2_migration_executor.py`, read/lock
only) ran against `visual_attendance_tier2_test` through the guard: one
consistent snapshot whose ledger rows equal the catalog baseline,
`ensure_ledger()` as a no-op under a role without `CREATE`, `migrate()` as
a no-op on a fully applied target, advisory-lock contention and release, a
deliberately failing migration that committed nothing, and a catalog
inventory (relations of every relevant kind including indexes, columns,
constraints, functions, non-internal triggers, policies, rules,
user-defined enum/domain/composite types, schemas, extensions, ledger
rows and count, employee count, guard rows and count) read in one
`REPEATABLE READ READ ONLY` transaction and identical before and after.

**Not verified, and not claimed:** a committed migration against any
database, application-database connectivity, and `apply` against a live
schema. Because the test role has no `CREATE`/`ALTER` privilege,
`ensure_ledger()`'s create path is fake-verified only. G7's exact scope is
stated in Section 3.7. G2's startup gate is implemented and offline-tested
(§3.8) but has **not** been run against an application database (§11.8).

### 11.8 Startup schema gate verification (G2, Step 38)

Offline only, and it stays that way until a separately authorized read-only
run. `tests/test_startup_schema.py` drives a recording fake connection and
pins: the one-connection order (connect → lock → snapshot → evaluate →
confirmed unlock → close), no snapshot after a refused lock, no retry, the
cleanup-never-masks-a-primary-refusal behavior, every policy code, and the
target-safety rules (both DSNs mandatory, malformed/ambiguous refused, the
test DSN never reaching an opener, local host aliases treated as one
server). `tests/test_api_startup_gate.py` pins guard → verification →
Uvicorn, release of the guard on refusal, that the production default is
the real verifier (no bypass), and the runner's exit codes `0`/`3`/`4` with
sanitized stderr. `ATTENDANCE_DATABASE_URL` was not contacted: no live
application-database startup verification has been performed.

## 12. Phase 5 P5.2 — identity schema (V002, implemented offline)

`migrations/V002__security.sql` adds two tables. It runs in a single
transaction, follows the V001 conventions (explicit `public.` qualification,
no `IF NOT EXISTS`, no database defaults for timestamps) and leaves `employee`,
its guard and the attendance journal untouched.

### 12.1 `app_user`

| Column | Type | Constraints |
| --- | --- | --- |
| `user_id` | `text` | primary key |
| `username` | `text` | not null (display form, case preserved) |
| `username_key` | `text` | not null, unique (`app_user_username_key_key`) |
| `password_hash` | `text` | not null, opaque until P5.3 |
| `role` | `text` | not null, `CHECK` in `Admin`/`HR Manager`/`Operator`/`Auditor` |
| `is_active` | `boolean` | not null, default `TRUE` |
| `created_at` | `timestamptz` | not null, application-authored |
| `updated_at` | `timestamptz` | not null, application-authored |

Usernames are normalised in the application (NFKC → trim → collapse internal
whitespace) and compared through `username_key`, the casefold of the display
form. The unique index on `username_key` is therefore the real identity
constraint; `user_id` is an opaque surrogate the application mints.

The role `CHECK` is the only database-level authorization fact. Permission
decisions per endpoint belong to P5.6.

### 12.2 `user_session`

| Column | Type | Constraints |
| --- | --- | --- |
| `session_token_hash` | `character(64)` | primary key (SHA-256 hex) |
| `user_id` | `text` | not null, `REFERENCES app_user(user_id)` |
| `issued_at` | `timestamptz` | not null, application-authored |
| `expires_at` | `timestamptz` | not null (sliding expiry) |
| `absolute_expires_at` | `timestamptz` | not null (immutable cap) |
| `revoked_at` | `timestamptz` | nullable (set on revoke) |
| `last_seen_at` | `timestamptz` | not null (activity tracking) |

Only the hash of a session token is stored. The plaintext token never reaches
the database, a log line or an exception message; hashing and issuance are
P5.4.

Indexes: `user_session_pkey` (primary key),
`user_session_user_id_idx` (revoke-all and per-user lookups, which the foreign
key alone does not serve) and `user_session_expires_at_idx` (expiry sweeps).
The foreign key has **no** `ON DELETE CASCADE` — accounts are deactivated with
`is_active`, never deleted, so session history is not silently dropped.

### 12.3 Contract and verification status

`src/schema_contract.py` declares both tables, their columns and primary keys,
`CONTRACT_MIGRATION_VERSION = 2`, and the required-table order `employee`,
`app_user`, `user_session`, `schema_version`. The contract cannot express
unique constraints, foreign keys, `CHECK`s or secondary indexes (the S3 reader
has no `secondary_indexes` fact), so those are asserted against the migration
text in `tests/test_migration_v002.py`; this is a documented contract
limitation, not a relaxed gate.

Offline verification only. `V002` has not been applied to any PostgreSQL
instance, and the repositories (`src/user_repository.py`,
`src/session_repository.py`, wired by `src/db_security_repository.py`) have
not been exercised against a live database. Foreign-key enforcement, `CHECK`
enforcement, index plans and case-insensitive uniqueness under the real
collation remain unverified until a separately authorized run.
