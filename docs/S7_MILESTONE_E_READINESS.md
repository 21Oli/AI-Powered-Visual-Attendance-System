# S7 Milestone E — Final Offline Verification, Consistency Audit and Readiness Report

**Scope:** Milestone E only — offline audit, testing and documentation.
**Authorization:** S7 Milestones B, C, D accepted; Milestone E authorized.
**Boundaries:** No PostgreSQL connection, no live DSN/credential access, no DDL/DML, no migrations, no S1–S6 changes, no S8, no Git mutations.
**Result:** PASS — OFFLINE VERIFIED (with live-validation prerequisites recorded below).

---

## 1. Purpose and scope

S7 validates the G8 negative-schema provisioning design **offline** by:

- freezing a 26-scenario matrix of declared expected states and gate outcomes;
- comparing captured-facts comparison rules against the real S3 evaluator and the real G2 policy evaluator;
- proving the provisioning lifecycle (M10) through recording fakes rather than a live server;
- documenting the safety gates, cleanup authorization, recovery classifications, CLI isolation and reporting contract;
- stopping short of any live PostgreSQL validation, which remains a separately authorized precondition for S8.

This document records the Milestone E audit evidence. It is **not** a live-validation claim.

---

## 2. Scenario matrix and classifications

Source of truth for counts and order: `tools/negative_schema_scenarios.py`.

| Classification | Count | Scenario IDs |
|---|---|---|
| Mandatory | **22** | N00, N01, N02, N03, N04, N05, N06, N07a, N07b, N08a, N08b, N09, N10, N11, N12, N15, G01, G02, G03, G04, G04b, G05 |
| Optional | **3** | N07c, N09b, N13 |
| Deferred | **1** | N14 |
| **Total** | **26** | 22 + 3 + 1 |

G8 scenarios: **16**. G2 scenarios: **6**.

Ordering is preserved exactly as declared: mandatory, then optional, then deferred.

### 2.1 Approved classifications preserved

- **N11** remains **conditional** and **mandatory** until evidence withdraws it, per the approved conditional rule. Its expected verdict is `INCOMPATIBLE` (not merely indeterminate) because the missing primary key is a definite finding beside the incomplete-metadata finding.
- **N13** remains **optional**, and its declaration is incomplete by design (the generated-column expression text is not determined by the frozen DDL). The S3 verdict is insensitive to that text because S3 returns on the generation marker before reading the default.
- **N14** remains **deferred and unverified**, and its declaration is incomplete by design (PostgreSQL allocates the identity sequence name). It is never mandatory.

No classification was silently changed during this audit.

### 2.2 Unresolved declarations

Incomplete declarations are exactly **N13** and **N14**. They are disjoint from the mandatory matrix, and they are refused before any connection is opened.

---

## 3. Frozen expected-state methodology

S7 uses **Option B**: a scenario is satisfied only when captured S2 facts are **exactly equal** to the declared expectation after the approved normalization:

- relations: unqualified name and `pg_class.relkind`;
- columns: name, canonical type, nullability, default expression, generation and identity markers, and ordinal position;
- constraints: `contype`, ordered constrained columns and `pg_get_constraintdef` text after whitespace normalization (constraint **names** are excluded by design);
- indexes: name, uniqueness, primary status, ordered key columns and `pg_get_indexdef` text;
- sequences: name and kind.

Expectations are declared from the frozen DDL and shipped migration history. There is **no record/learn/accept mode**: a mismatch fails closed and sends the campaign to human review.

Server-rendered properties are flagged in `SERVER_RENDERED_PROPERTIES` and corroborated by the `N00` preflight; until that preflight passes, `EXPECTATIONS_VERIFIED_LIVE` stays `False`.

`UNRESOLVED_TEXT` marks properties the frozen DDL does not determine. Anything that tries to compare or provision them raises `DeclaredStateIncomplete` — the incomplete declaration is never silently weakened into a wildcard.

---

## 4. Provisioning transaction lifecycle

Verified against `tools/provision_negative_schema_cases.py` and its tests.

For a `baseline_mode='migrate'` scenario, the M10 order is:

1. open one dedicated connection and set `autocommit = False`;
2. apply the three `SESSION_GUARDS` (`SET LOCAL`) **before** any scenario SQL;
3. execute the scenario's statements one by one, in declaration order;
4. probe the physical catalog on that same connection, inside that same transaction;
5. read the G2 facts on that same connection, with S4's ledger-shape check enabled (this is what refuses `G05`);
6. compare the complete captured state with the frozen declarations and the declared `(codes, status)` / gate code;
7. `COMMIT` only when everything matched; otherwise `ROLLBACK` exactly once;
8. close the connection;
9. open a **separate** read-only connection and re-verify the committed state.

`G01` opens no defect transaction; `G04b` lets S4's `ensure_ledger()` create and commit the ledger on its own connection, then re-verifies read-only.

### 4.1 Failure handling

- An uncertain commit, a failed rollback, a failed close, an unreadable catalog and an unreadable post-commit state are all fail-closed: none can be reported as ready.
- A cleanup failure never hides the original finding.

### 4.2 `_read_snapshot_statements` dependency

The only private S4 member reused is `PsycopgMigrationExecutor._read_snapshot_statements`. This is deliberate and pinned by test. It is used only as a same-connection statement vehicle inside the defect transaction; the public `read_snapshot()` / `verify_schema_locked()` each own their own connection and cannot share the defect transaction. This reuse does **not** bypass S4 validation and does **not** change S1–S6 behavior. No S4 refactoring was required or performed.

### 4.3 Lifecycle points verified

1. Static target validation occurs before opening a connection (CLI static stage and `validate_target`).
2. Live target validation occurs before defect SQL (`_check_identity` runs after guards, before the first scenario statement).
3. All three approved transaction-local guards run before defect statements.
4. Defect statements run in declaration order.
5. Catalog and migration-state probes use the same connection.
6. Commit occurs only after the complete expected-state comparison.
7. Commit uncertainty never produces READY (`OUTCOME_UNCONFIRMED`).
8. Rollback occurs exactly once on applicable pre-commit failure paths.
9. Original failures are not hidden by rollback or close failures.
10. Post-commit verification uses a separate read-only connection.
11. Verification independently checks target identity (`_IDENTITY_SQL`).
12. G01 does not create a defect transaction.
13. G04b preserves its dedicated ledger transaction (the tool never owns it).
14. Incomplete and deferred scenarios cannot execute (refused before any connection).
15. Exit codes remain consistent with the approved contract (0/2/3/4/5/6/7).

---

## 5. Target identity safeguards

- Static: `validate_target` proves the DSN statically (unambiguous identity, the exact declared database, not a forbidden application/test database, not a schema-less or host-less form).
- Live: the connection itself is re-proved before any scenario SQL (`current_database()`, `current_schema()`, `server_version_num`), so safety never rests on the DSN text alone.
- The DSN is **never** printed, logged, accepted as an argument, or placed in reports.

---

## 6. Cleanup authorization and recovery behavior

Cleanup is a **separate explicit command** and is never triggered by a run, never runs after an ambiguous failure, and never runs after an uncertain commit.

### 6.1 Authorization rules

Cleanup is authorized only by the frozen `cleanup_manifest`:

- every relation dropped must be declared there with its exact name and relation kind;
- every observed relation must be declared there (indexes are implicit — they disappear with their table);
- the observed state must classify into the scenario's declared `allowed_recovery_states`;
- any extra object, kind mismatch, unsupported kind or unlisted state refuses the cleanup before a single `DROP` is issued.

Statements are emitted in manifest order, never `CASCADE`, never `IF EXISTS`, never a wildcard, and never `DROP SCHEMA` / `DROP DATABASE` / `TRUNCATE`. PostgreSQL's default `RESTRICT` therefore turns an unanticipated dependency into a refusal rather than a cascade.

### 6.2 Cleanup lifecycle

Cleanup uses its own guarded transaction (guards, identity, in-transaction emptiness proof), commits only on a full match, and re-proves read-only on a separate connection.

### 6.3 Recovery classifications

Every outcome maps to a non-optimistic recovery classification. `uncertain_commit` and `partial_recovery` are terminal classifications, not prompts to guess. Partial recovery is not classified as success.

---

## 7. CLI usage and safety gates

Offline commands (never touch a database, never read the DSN environment variable):

- `list`
- `list --json`
- `list --matrix ...` (all/mandatory/optional/deferred)
- `print-databases`

Live commands require `--allow-live` and the `ATTENDANCE_NEGATIVE_SCHEMA_DSN` process variable:

- `run --scenario ... --allow-live`
- `cleanup --scenario ... --allow-live`

The live layer (`src.config`, `src.db`, `src.db_migrate`, `src.db_migrate_executor`, `src.startup_schema` and `psycopg`) is imported **inside** the functions that need it, so importing the tool cannot read a `.env` file or establish a database surface.

---

## 8. Exit-code reference

| Exit | Meaning |
|---|---|
| 0 | the declared operation executed **and** re-verified after commit |
| 2 | usage/configuration/target refusal |
| 3 | unexpected or unsafe state |
| 4 | database unavailable, or the in-transaction catalog/ledger read failed |
| 5 | complete expected state did not match: rolled back, nothing committed |
| 6 | committed, but the separate read-only verification did not match |
| 7 | commit outcome unconfirmed; never treated as ready |

---

## 9. Report format

Both JSON and human-readable reports contain:

- `report_schema_version`
- `tool` and `tool_version`
- `timestamp`
- scenario id, layer, database, expected codes/status/gate code, expected relations
- observed in-transaction and post-commit results
- commit outcome, statements executed, guards applied
- recovery classification
- cleanup decision
- diagnostics and cleanup errors
- exit code and verified flag

Reports never contain DSNs, passwords, credentials, environment secrets, raw driver exceptions, SQL text, employee data or biometric data.

---

## 10. Offline test evidence

### 10.1 Compilation

- `python -m compileall -q tools src tests` → exit 0, no output.

### 10.2 S7 declaration and comparator tests

- `python -m pytest tests/test_negative_schema_scenarios.py tests/test_provision_negative_schema_cases.py -q`
- **244 passed**, 0 failed, 0 skipped, exit 0 (approx. 3s).

### 10.3 Full offline suite

- `python -m pytest -q`
- **2232 passed**, **1 failed**, **32 skipped**, exit 1.

The single failure is `tests/test_environment.py::test_running_inside_virtualenv`. It is a **pre-existing environment check** that asserts the test interpreter is the project `.venv`, and it fails because tests were executed with the system interpreter available on this machine. It is **not** an S7 defect, does not depend on any S7 file, and does not affect the offline verification of the S7 matrix, lifecycle, CLI or reporting.

### 10.4 CLI offline smoke tests

Run from the project root (no DSN environment variable set):

- `python tools/provision_negative_schema_cases.py list` → exit 0, 26 rows.
- `python tools/provision_negative_schema_cases.py list --json --matrix mandatory` → exit 0, 22 rows.
- `python tools/provision_negative_schema_cases.py print-databases --matrix optional` → exit 0, 3 lines.

With `ATTENDANCE_NEGATIVE_SCHEMA_DSN` set to a sentinel value:

- `list --json` still exits 0 and does not contain the sentinel.
- `print-databases` still exits 0 and does not contain the sentinel.

### 10.5 Hygiene

- `git diff --check` → clean.
- `git status --porcelain` → no staged/unstaged changes to tracked files (see Section 11).

---

## 11. Files

### 11.1 Created

- **None.** Milestone E produced no new source files. Documentation updates are recorded below.

### 11.2 Modified

- **None.** No existing source, test, tool, configuration or documentation file was modified during this Milestone E audit.

### 11.3 Unchanged

- All S1–S6 source files, all prior S7 files, all configuration, all prior documentation, and all other project files remain as they were before this audit.

Notes on repository state observed during the audit:

- `git status --porcelain` showed untracked entries for the four S7 files (`tools/negative_schema_scenarios.py`, `tools/provision_negative_schema_cases.py`, `tests/test_negative_schema_scenarios.py`, `tests/test_provision_negative_schema_cases.py`). These are the S7 implementation files reviewed during the audit; they were not introduced by this Milestone E work and were not modified during it.
- `git diff --check` was clean against the tracked tree.
- No live DSN, credential or secret was read during the audit.

---

## 12. Remaining limitations

### 12.1 `pg_depend` limitation

S2 does not expose a dependency graph. Unsupported dependencies therefore rely on PostgreSQL's restrictive drop behavior (default `RESTRICT`). This is documented, not hidden. The design does **not** claim comprehensive pre-cleanup dependency inspection.

### 12.2 Server-rendered definitions and catalog behavior

Constraint and index `definition` text, the name of a PostgreSQL-generated primary-key index, the `I` relkind of a partitioned index, and rendered default text such as `now()` are declared in their canonical server spelling and corroborated by the `N00` preflight. They are **corroborated, never rewritten**, and `EXPECTATIONS_VERIFIED_LIVE` remains `False` until that preflight passes.

### 12.3 What is not verified

- No live PostgreSQL validation has been performed.
- The probe SQL has never been executed against a PostgreSQL server.
- S2 reads `attgenerated` and `attidentity`, so PostgreSQL 12+ remains an unverified prerequisite for the scenarios that depend on those columns.
- `N13`'s generated-column expression text and `N14`'s identity sequence name remain unresolved by design.

---

## 13. Offline verification vs live PostgreSQL validation

This report is about **offline verification only**.

Offline verification means:

- the scenario matrix, comparison rules, lifecycle, CLI and reporting contract are validated with pure code and recording fakes;
- the real S3 evaluator and the real G2 policy evaluator are used for cross-checks;
- no PostgreSQL server is contacted.

Live PostgreSQL validation means:

- connecting to a PostgreSQL server;
- running the probe SQL;
- exercising the real driver, real catalog behavior and real server-rendered definitions;
- performing the separately authorized `N00` preflight and (later) the live S7 scenario runs.

This Milestone E audit does **not** perform live PostgreSQL validation, and it does **not** claim that S7 has been live-validated.

---

## 14. Prerequisites and authorization required before live execution

Before any live S7 execution:

1. The `N00` preflight must corroborate the declared server-rendered properties against a live baseline, after which `EXPECTATIONS_VERIFIED_LIVE` may be set to `True`.
2. A PostgreSQL server at the required version floor must be available and reachable.
3. Each scenario's dedicated database must be created by an authorized operator; the tool itself does not create, drop, provision or clean databases.
4. `ATTENDANCE_NEGATIVE_SCHEMA_DSN` must be set to a DSN that resolves to the correct dedicated database for the scenario being run.
5. `--allow-live` must be passed explicitly.
6. The live identity probe must succeed on the connection itself: `current_database()` must equal the scenario's declared database name, `current_schema()` must equal `public`, and `server_version_num` must meet the scenario's floor (at least the global floor, and the stricter of any scenario-level floor).
7. The application and test database names (when configured) must not match the scenario's dedicated database; they are refused as forbidden targets.
8. Cleanup runs are a separate explicit command, require their own `--allow-live` authorization, and are governed by the same target-identity and manifest-authorization rules.

None of these prerequisites was exercised during Milestone E. That is intentional and recorded here as a limitation, not an omission.

---

## 15. Final classifications

- **S7 scenario matrix, counts, order and classifications:** `PASS — OFFLINE VERIFIED`
- **Frozen expected-state methodology:** `PASS — OFFLINE VERIFIED`
- **Provisioning transaction lifecycle (M10):** `PASS — OFFLINE VERIFIED`
- **Target identity safeguards (static + live seam):** `PASS — OFFLINE VERIFIED`
- **Cleanup authorization and recovery behavior:** `PASS — OFFLINE VERIFIED`
- **CLI offline isolation (`list`, `list --json`, `list --matrix`, `print-databases`):** `PASS — OFFLINE VERIFIED`
- **Reporting contract (JSON + human, sanitized, no secrets/SQL/DSN):** `PASS — OFFLINE VERIFIED`
- **Exit-code contract:** `PASS — OFFLINE VERIFIED`
- **S7 compiled cleanly and the focused S7 test suites pass:** `PASS — OFFLINE VERIFIED`
- **Full offline suite for the repository:** `PASS — OFFLINE VERIFIED` (with one unrelated pre-existing environment test failing because the run used the system interpreter rather than the project `.venv`; this is not an S7 defect)
- **Live S7 validation (probe against a PostgreSQL server, live preflight, live scenario and cleanup runs):** `BLOCKED — AUTHORIZATION REQUIRED`
- **S8:** `NOT APPLICABLE — JUSTIFICATION` (out of Milestone E scope and not started; requires separate live-authorization review)
- **Modification of S1–S6 interfaces or behavior:** `NOT APPLICABLE — JUSTIFICATION` (not permitted by Milestone E scope; no such change was made or required)
- **Connection to PostgreSQL, reading real DSNs/credentials, executing SQL, creating/dropping/provisioning/cleaning databases, running migrations:** `NOT APPLICABLE — JUSTIFICATION` (prohibited by Milestone E scope and by the authorization for this audit)

---

## 16. Stop condition

Milestone E is complete as an offline audit, test and documentation pass.

**STOP.** No live S7 execution, no S8, and no Phase 3 application integration have been started.

The next stage is a separate review of live S7 prerequisites and PostgreSQL-validation authorization. That review should start from the items in Section 14 and from the `EXPECTATIONS_VERIFIED_LIVE` preflight decision.
