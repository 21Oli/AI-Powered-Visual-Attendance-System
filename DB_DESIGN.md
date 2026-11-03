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
runtime wiring, any Tier-2 integration test (requires the
owner-provisioned dedicated test database with an established marker,
Step 1.5b-3), and the Step 1.5c importer.

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
* The runner is the only permitted writer of schema DDL; the application
  may not serve requests while the runner owns the connection (runtime
  contract: migrate → then serve; enforced in the Step 1.5b integration
  wiring).

### 3.3 Partial-failure handling

* A failure mid-script rolls that migration's transaction back entirely;
  the database remains at its previous version, unmodified by the failed
  script. Nothing is marked "partially applied".
* Any failed migration **aborts startup** with a single sanitized line
  ("migration <N> failed; refusing to start the API") — never a raw
  driver message, never a DSN. Trusted detail stays server-side in the
  log via the existing `src.logging_setup` contract, following the Gate
  1.2 redaction discipline and the same "no traceback to the console"
  pattern the runner already uses for configuration (exit 2) and guard
  (exit 3) failures.
* Retrying a failed migration is an operator decision, never automatic
  (same no-implicit-retry rule as `src/db.py` connections and
  `PERSISTENCE_UNCONFIRMED` writes).

### 3.4 Migration status reporting (no credential leakage)

Migration status output shows only: target verified?, current version,
applied version list, and **sanitized error classes** — never a driver
exception string, never the DSN. Wherever a DSN could reach output, the
existing `src/db.py` `_redact()` machinery is reused (D4).

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
  offline-tested in `src/db_migrate.py`; **not** exercised against a live
  server (no psycopg-backed executor exists yet). See
  DEVELOPMENT_STATUS.md Step 31.
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
  goes through the re-used `src/db.py` connection layer **in the later
  runtime-wiring step** — not implemented in 1.5b. Not executed against
  PostgreSQL during 1.5b.

## 4. Employee repository interface (Section C)

**Step 1.5b status: implemented offline** in
`src/employee_repository.py`, tested against injected fakes
(`tests/test_employee_repository.py`). No database has been connected
to, so the persistence behavior is contract-verified only. The
remaining unimplemented parts of this design are the API endpoints that
would consume the repository (a later gate).

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
committing. **No PostgreSQL server has been contacted**: no marker
exists anywhere yet, and the chain has never run against a live target.

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
| 1.5c-1 | **Employee-metadata export** from the enrollment manifests (read-only) | `src/employee_export.py`, `tools/export_employee_metadata.py`, `tests/test_employee_export.py` | No | **Implemented (offline) in Step 1.5c-1** |
| 1.5c-2 | Importer over the exported JSONL (fake-backed tests only) | proposed module + tests | No | Owner approval of the import behavior (duplicate / conflict / partial-failure policy) |
| 1.5c-3 | Real import into PostgreSQL | none (owner-run) | **Yes** | Requires 1.5b-3 (provisioned DSN + marker) and the psycopg-backed runtime wiring |
| 1.5c+ | DB-backed API endpoints; checkout endpoint | not yet designed | Yes | later gate, separate design |

**Acceptance criteria before any Step 1.5 code milestone is accepted:**

* New modules import side-effect free; no `psycopg` import at module
  load; no blank-DSN fallback (hygiene-test-enforced, extending the
  existing patterns).
* Tier 1 fully green, offline.
* Tier 2 green only under the Section 5.2 contract; an
  unmet/ambiguous condition produces the typed BLOCKED, not a weakened
  assertion.
* Existing suites remain green (265 attendance core + 120 config/db/API
  + 14 guard + 153 integration, as last verified at `bb95559`).
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

* The importer itself (Step 1.5c-2): duplicate/conflict/partial-failure
  policy for `EmployeeRepository.create` (`skip-if-identical` vs.
  `abort`), and whether a conflict report may name fields.
* The real import (Step 1.5c-3): requires the owner-provisioned dedicated
  test database, its `_phase3_test_guard` marker, and the psycopg-backed
  runtime wiring that does not exist yet. Tier-2 verification remains
  outstanding.
* Reconciliation of the interim divergence between manifest-based
  recognition gating and future database reads (Section 9.1), and the
  treatment of never-enrolled/deleted employees.
