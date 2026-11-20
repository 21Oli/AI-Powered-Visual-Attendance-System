# P5.6 — Acceptance evidence

Milestone: **P5.6 — user administration, secure first-Admin provisioning, and the
Tier-2 cleanup-permission corrections**
Repository: `21Oli/AI-Powered-Visual-Attendance-System-v2`
Evidence date: 2026-11-20

## 0. Status

**Source corrections are complete and every offline gate is green. Real-PostgreSQL
concurrency verification is still BLOCKED.** The seven concurrency scenarios in
`tests/test_tier2_user_concurrency.py` are reported as *skipped* (never as
passed), because the dedicated test database still has no V002 schema and no
`attendance_tier2_cleanup` role, and this task carried no authorization to create
roles, issue `GRANT`/`REVOKE`, or execute the owner script.

This document records evidence. It does **not** accept P5.6 on the owner's
behalf, and P5.7 has not been started.

## 1. What the milestone implements

* `src/user_service.py` — `UserService` with `list_users`, `create_user`,
  `change_user_role`, `deactivate_user`, `provision_first_admin`; refuses
  non-Admin actors, self-role-change and self-deactivation; hashes with the
  existing Argon2id service; never returns a hash or credential.
* `src/user_repository.py` — the last-active-Admin invariant is decided inside
  one database transaction (`SET TRANSACTION ISOLATION LEVEL READ COMMITTED`,
  then `pg_advisory_xact_lock`, then read-check-write), not by a Python
  read-then-write.
* `src/api_user.py`, `src/api.py` — four Admin-only endpoints guarded by
  `users:manage`; `APPROVED_ROUTES` grows from 11 to 15; a restricted audit
  subsystem answers `503 AUDIT_UNAVAILABLE` before any mutation.
* `src/user_provision.py` — operator-only CLI, no HTTP route, no default
  credentials, password via `getpass`, identity-only output.
* Audit — durable intent before every mutation and a correlated result after it;
  no password or hash is ever recorded.
* **This correction round** — effective-privilege verification for the two
  PostgreSQL test roles, cluster-identity verification in the shared guard, and
  the `pg_get_userbyid(grantee)` defect in the owner script.

## 2. Files changed in this correction round

| File | Change |
| --- | --- |
| `tools/apply_v002_tier2_test_database.sql` | Replaced the `pg_get_userbyid(c.grantee)` comparison with a direct role-name comparison; added block 5n-bis (runtime role holds no column-level grant); rewrote 5o/5p as *effective* privilege checks (`has_table_privilege`, `has_column_privilege` over every catalog column, column-level write rejection); updated the header and the closing `NOTICE`. The embedded V002 block, the transaction structure, the database-name check and the `system_identifier` pin are unchanged. |
| `src/test_database_guard.py` | `_read_system_identifier()` hardened: a raised query, a missing row, a missing column, `None`, a boolean, a non-integer, `0` and negative values are all "unavailable" and produce `IDENTITY_UNVERIFIABLE`. Docstring corrected. |
| `tests/test_tier2_user_concurrency.py` | `_require_cleanup_credential()` rewritten: effective table privileges for **both** Tier-2 roles, per-column effective `SELECT` for the cleanup role, column-level write rejection, raw column-ACL records for the runtime role, mutual-membership rejection, and a fail-closed message when `pg_control_system()` cannot be executed. Cluster identity still compared against `VerifiedTestTarget.system_identifier`. |
| `tests/test_cleanup_credential_verification.py` | Rewritten: 19 offline regression tests covering the minimum permission set plus every privilege-expansion and identity failure mode. |
| `tests/test_apply_v002_owner_script.py` | Removed the assertion that enshrined the defective `pg_get_userbyid(c.grantee)` expression; added regression tests for role-name comparison (no `pg_get_userbyid` on `grantee`, no `::regrole`), effective `DELETE` verification and effective per-column `SELECT` verification. 15 tests. |
| `tests/test_test_database_guard.py` | Fake connection can raise on the `pg_control_system()` read; new tests for malformed/zero/negative/string/float identifiers, for a read failure, and for the read happening on the read-only connection. 58 tests. |
| `docs/PHASE5_IMPLEMENTATION_PLAN.md` | §10.1 runbook: least-privilege grant set, effective-privilege verification block, `grantee`-as-name rule, `pg_control_system()` executability evidence, test inventory. |

No migration file changed. `migrations/V002__security.sql` is untouched
(`35bc00cd7a947a9eca31a7502d3a17e0ff55163f1c5222cf8fcb88a6328082d3`), as is
`migrations/V001__employee.sql` (checksum in the §10.1 runbook).

## 3. Security protections

* **Authorization.** The backend is the only authority: the four user routes
  require `users:manage` through the existing `permission_dependency` →
  `make_session_dependency` chain, and `UserService` independently refuses a
  non-Admin actor, a self role change and self-deactivation.
* **Last-active-Admin invariant.** Enforced inside one transaction with an
  advisory lock, at `READ COMMITTED`, so the waiting transaction re-reads
  committed rows instead of taking a stale decision. Scenario 7 proves it
  against a session whose default is `REPEATABLE READ`.
* **Audit.** Intent-then-result with correlation; unavailable audit blocks the
  mutation; only provably-not-applied errors are reported as definite failures;
  no credential, hash, token or biometric material in any record.
* **Database guards.** The shared guard is unchanged in contract: explicit test
  DSN required, application DSN required for the distinctness proof, enforced
  read-only mode *before* the first verification query, server-side marker plus
  ledger cross-check, and one connection that is always closed.
* **Least privilege.** The runtime role has `SELECT`/`INSERT`/`UPDATE` and no
  `DELETE`; the test-only cleanup role has `DELETE` on exactly the two security
  tables plus column-level `SELECT` on `user_id`, and nothing else.
* **No secrets in the repository or in logs.** The cleanup credential is read
  only from `ATTENDANCE_TIER2_CLEANUP_DATABASE_URL`; no DSN, host, user or
  password appears in any error message produced by the guard or the suite.

## 4. SQL permission verification (owner script)

Block 5 previously compared `pg_get_userbyid(c.grantee)` to a role name. In
`information_schema.column_privileges`, `grantee` is **already a role name**
(`sql_identifier`), not a `pg_authid.oid`, so that expression is a type error:
the block either failed the whole apply or compared nothing. It is now
`c.grantee = 'attendance_tier2_runtime'`. The only remaining
`pg_get_userbyid()` call resolves `pg_class.relowner`, which genuinely is an OID,
and `tests/test_apply_v002_owner_script.py` fails if `grantee` is ever passed to
it again (or if `::regrole` appears in the script).

The corrected verification blocks assert **effective** privilege:

| Check | Mechanism | Required |
| --- | --- | --- |
| runtime `SELECT`/`INSERT`/`UPDATE` | `has_table_privilege` | present on both tables |
| runtime `DELETE` | `has_table_privilege` | absent on both tables |
| runtime column-level grants | `information_schema.column_privileges` (visible to the granting owner) | none at all |
| cleanup `DELETE` | `has_table_privilege` | present on both tables |
| cleanup table-level `INSERT`/`UPDATE` | `has_table_privilege` | absent |
| cleanup `SELECT(user_id)` | `has_column_privilege` per table | present in both tables |
| cleanup `SELECT` elsewhere | `has_column_privilege` over **every** catalog column | absent — including table-level `SELECT`, another column, or an inherited grant |
| cleanup column-level writes | `has_column_privilege` per column | absent |

`has_column_privilege()` succeeds when the privilege is held for the whole table
or granted for that column, so the per-column sweep is what makes a table-level
`SELECT` grant detectable *and* what makes an inherited grant visible. The column
list comes from `information_schema.columns`, so a future migration's columns are
covered without editing the script. `has_table_privilege()` is deliberately not
used for the cleanup role's table-level `SELECT` assertion: it reports the
table-level ACL only, and the effective read surface is already asserted per
column.

Preconditions (database name, `system_identifier`, executing authority, V001
baseline, absent objects, role shape, no mutual membership) and the
single-transaction `BEGIN`/`COMMIT` with rollback on any raised exception are
unchanged. No precondition was weakened.

`tests/test_apply_v002_owner_script.py` also keeps the embedded V002 block
byte-identical to `migrations/V002__security.sql` and the grant set at exactly
six statements.

**Not executed:** the script has not been run against any database in this round.
It needs a role with `CREATE` on schema `public`, which the agent does not have
and was not authorized to use.

## 5. Guard and cluster-identity verification

* `VerifiedTestTarget` carries `system_identifier: int`, captured from the
  verified connection (`SELECT system_identifier FROM pg_control_system()`), and
  is still obtainable only through `verify_test_database()` (direct construction
  raises `ValueError`; the private `_TARGET_PROOF` is the cooperative gate).
* Fail-closed value handling: a raised query, missing row, missing column,
  `None`, boolean, string, float, `0` or negative value is *unavailable* and
  produces `IDENTITY_UNVERIFIABLE`. The identifier is never defaulted.
* The cleanup credential is verified against the *verified* target on three
  axes: database name, cluster `system_identifier`, and the expected role name
  being a non-superuser. A matching database name is never accepted as proof of
  the same cluster.
* Neither Tier-2 role may be a member of the other (`pg_has_role ... 'MEMBER'`),
  because membership alone would hand the application role `DELETE` or the
  teardown role write access.
* A privilege that cannot be inspected fails the run. In particular, if the
  cleanup credential cannot execute `pg_control_system()`, the suite fails with
  an explicit "cluster identity cannot be proven" message instead of falling
  back to a name-only match.

**Executability of `pg_control_system()` for the intended roles.** PostgreSQL
grants `EXECUTE` on functions to `PUBLIC` by default and the control-data
functions are not restricted, so no superuser and no `pg_monitor` membership is
needed. Positive evidence on this cluster: the pinned identifier
`7687968999847354228` was captured read-only through the **non-superuser**
`attendance_tier2_runtime` session (recorded in §10.1 of the implementation
plan on 2026-11-19, together with `current_user = attendance_tier2_runtime` and
`rolsuper = false`). The same function is read by the guard on the verified
connection, which is why an `EXECUTE` revocation fails closed rather than being
silently skipped. No superuser, monitoring or extra membership was granted to
obtain this.

## 6. Test results

### 6.1 Offline (no database connection, no credential)

| Suite | Command | Result |
| --- | --- | --- |
| Owner-script static review | `.venv/Scripts/python.exe -m pytest tests/test_apply_v002_owner_script.py -q` | **15 passed** |
| Test-database guard | `... tests/test_test_database_guard.py -q` | **58 passed** |
| Cleanup-credential regression | `... tests/test_cleanup_credential_verification.py -q` | **19 passed** |
| Tier-2 harness control | `... tests/test_tier2_harness_control.py -q` | **25 passed** |
| Tier-2 concurrency (opt-in absent) | `... tests/test_tier2_user_concurrency.py -q` | **7 skipped, 0 passed** |
| Audit API file | `... tests/test_api_audit.py -q` | **40 passed** |
| Audit group (api + service + writer + exclusion) | `... tests/test_api_audit.py tests/test_audit_service.py tests/test_audit_writer.py tests/test_audit_exclusion.py -q` | **208 passed** |
| Targeted audit privacy test | `... "tests/test_api_audit.py::test_no_secrets_or_biometric_material_in_records" -q` | **1 passed** |
| Combined focused run | `... tests/test_apply_v002_owner_script.py tests/test_test_database_guard.py tests/test_cleanup_credential_verification.py tests/test_tier2_harness_control.py tests/test_tier2_user_concurrency.py -q` | **117 passed, 7 skipped** |
| Python compile check | `... -m compileall -q src/test_database_guard.py tests/test_cleanup_credential_verification.py tests/test_tier2_user_concurrency.py tests/test_apply_v002_owner_script.py tests/test_test_database_guard.py` | **exit 0** |
| Whitespace check | `git diff --check` | **exit 0** |

### 6.2 Complete backend suite

| Run | Command | Result |
| --- | --- | --- |
| Before this correction round (tree as the round started) | `.venv/Scripts/python.exe -m pytest -q` | **3984 passed, 40 skipped, 0 failed** (399.62 s) |
| After this correction round | `.venv/Scripts/python.exe -m pytest -q` | **4002 passed, 40 skipped, 0 failed** (369.02 s) |
| Final tree (after the explicit-cast hardening pass), repeated | `.venv/Scripts/python.exe -m pytest -q` | **4002 passed, 40 skipped, 0 failed** (355.16 s) |

The +18 delta is exactly the tests added in this round (owner-script +5,
guard +7, cleanup-credential rewrite +9 net), which is a useful consistency
check on the totals.

### 6.3 Full-suite inconsistency from the previous report — resolution

* The previous report's two figures (`3963 passed / 33 skipped / 11 failed` and
  `3973 passed / 33 skipped / 1 audit failure`) both total 4007 items, i.e. they
  were two runs of the *same* tree, and neither was green.
* The 11 failures were the harness-control failures caused by the newly required
  `system_identifier` field; they were fixed in the previous round (25 passed
  after the fix), and the subsequent correspondences were taken from focused
  runs rather than from a clean full run, which is where the reporting
  inconsistency came from.
* The audit failure was **not** labelled pre-existing. What was actually checked
  in this round:
  * the failing test alone: **1 passed**;
  * the whole audit file: **40 passed**;
  * the four-module audit group: **208 passed**;
  * the complete suite, three times (before the round, after it, and again on
    the final tree): **0 failures** each.
* Mechanism review: `test_no_secrets_or_biometric_material_in_records` builds
  its own app at a unique `tmp_path`, writes and reads only
  `tmp_path/audit/audit.jsonl`, and touches no module-level state, so no
  shared-state or ordering dependency could be identified. The failure did not
  reproduce in two complete runs at the current tree; the cause of that single
  earlier failure remains **unexplained**, and it is reported as such rather
  than as "flaky" or "pre-existing".

### 6.4 Real PostgreSQL (opt-in)

| Suite | Result |
| --- | --- |
| `tests/test_tier2_user_concurrency.py` with `ATTENDANCE_TIER2_OPT_IN=1` | **NOT RUN — BLOCKED** (see §7) |

Offline suites prove the *verification logic* only. They are not evidence that
the PostgreSQL concurrency scenarios pass, and no mock or sequential rewrite of
those scenarios was substituted.

## 7. PostgreSQL verification — executed vs not executed

**Executed (read-only, no credential):**

* Attempted a direct client probe of the dedicated database:
  `psql -X -A -t -w -d visual_attendance_tier2_test -c "SELECT ..."` →
  `connection to server at "localhost" (::1), port 5432 failed: fe_sendauth: no
  password supplied`. The task forbids reading `.env`/credential files, so no
  configured DSN was obtained, and no further connection attempt was made.

**Not executed, and why:**

| Item | Status | Reason |
| --- | --- | --- |
| Reconfirm test-database identity and V001 baseline/checksum | BLOCKED | no authorized credential path |
| Reconfirm `system_identifier` | BLOCKED | same; the pinned value from 2026-11-19 is unchanged and still asserted by the script |
| Confirm the exact roles and privileges on the server | BLOCKED | same |
| Confirm the cleanup connection can read `pg_control_system()` | BLOCKED | the role does not exist yet; the default `PUBLIC` `EXECUTE` right plus the earlier non-superuser capture is the current evidence |
| Execute `tools/apply_v002_tier2_test_database.sql` | NOT EXECUTED | needs a role with `CREATE` on schema `public` and explicit owner authorization |
| `CREATE ROLE attendance_tier2_cleanup` | NOT EXECUTED | role creation is an owner action; not authorized in this task |
| Issue any `GRANT`/`REVOKE` | NOT EXECUTED | explicitly out of scope without owner authorization |
| Run the 7 concurrency scenarios | NOT RUN | blocked by the three items above |
| Access `ATTENDANCE_DATABASE`, production or application data | NOT ACCESSED | prohibited |

The corrected SQL verification blocks (5n-bis, 5o, 5p) are therefore
**statically reviewed and unit-tested, but not executed** against PostgreSQL in
this round.

## 8. Remaining blockers and safe manual prerequisites

Owner steps, in order (no step is optional):

1. `CREATE ROLE attendance_tier2_cleanup LOGIN PASSWORD '<owner-chosen>';`
   — an ordinary non-superuser login role, no `CREATEROLE`/`CREATEDB`, no
   membership in `attendance_tier2_runtime` (and none in the other direction).
2. Put its DSN in the local, untracked environment as
   `ATTENDANCE_TIER2_CLEANUP_DATABASE_URL`. Never in a tracked file, never in
   chat.
3. `sha256sum migrations/V002__security.sql` must print
   `35bc00cd7a947a9eca31a7502d3a17e0ff55163f1c5222cf8fcb88a6328082d3`.
4. Run, as **one** script with a role that has `CREATE` on schema `public`:
   `psql -v ON_ERROR_STOP=1 --dbname "<owner DSN for visual_attendance_tier2_test>" --file tools/apply_v002_tier2_test_database.sql`
5. If the cluster has revoked `EXECUTE` on `pg_control_system()` from `PUBLIC`,
   do **not** grant monitoring or superuser to work around it: that revocation
   makes the cluster-identity check unprovable, and both the guard and the suite
   fail closed by design. Record it as a blocker and verify identity another
   reviewed way.
6. `ATTENDANCE_TIER2_OPT_IN=1 .venv/Scripts/python.exe -m pytest tests/test_tier2_user_concurrency.py -q`
   and record the output.

Until step 6 passes, the concurrency claim rests on the advisory-lock design and
the offline tests only.

## 9. P5.7 prerequisites

P5.7 (frontend authentication and session integration) must not begin until:

1. this correction round is reviewed and accepted by the owner;
2. the owner has either completed steps 1–6 above, or explicitly accepted P5.6
   with the PostgreSQL concurrency verification recorded as BLOCKED;
3. the owner has explicitly authorized starting P5.7.

No P5.7 or P5.8 implementation was started, and no frontend file was touched.
