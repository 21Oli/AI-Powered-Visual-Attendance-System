# S8-CONTRACT 1.2 — read-only schema and migration integrity verification

Status: **proposed revision 3** (supersedes revision 2 / `S8-CONTRACT 1.1`).
Scope: the contract S8 must satisfy. Implementation: `src/s8/verification.py`,
`tools/verify_schema_s8.py`, `tests/test_s8_verification.py`.

This revision keeps every architecture decision already approved and applies the
six mandatory corrections C1–C6. Where revision 2 was self-contradictory, the
contradiction is named and resolved; nothing is left to the implementer's
discretion.

---

## 1. Purpose and non-goals

S8 answers exactly one question, with evidence:

> Does the owner-designated database still hold the schema and the recorded
> migration history that the shipped code requires?

S8 does **not** migrate, repair, reconcile, provision, seed, lock, or clean. It
does **not** establish that the application is correct, that the camera pipeline
is accurate, that the role has least privilege, or that no concurrent migration
was running.

The honest limitation is therefore part of the report, not a footnote:

> REPEATABLE READ gives one consistent snapshot. It does not serialize this
> verification against a concurrent migration. A migration committed after this
> snapshot began may not be reflected in this report.

---

## 2. Target designation (C3)

Revision 2 was ambiguous between §2.1 ("the configured application target is the
subject") and §2.2 ("an explicit DSN may be supplied"). The two sections now say
the same thing.

| Term | Meaning | Mandatory? |
|---|---|---|
| `settings.database_url` | the **configured application identity** | **yes**, for any positive proof |
| `designated target` (`--target`) | optional restatement of that identity | no |
| `settings.test_database_url` | comparison-only test target | no |

Rules:

1. The application DSN is **mandatory**. It is the only thing that can positively
   prove "this is the application database".
2. An explicit target may restate the application identity. It must normalize to
   the **same** `(dbname, host, port)` identity; if it does not, the run is
   `BLOCKED — TARGET NOT VERIFIED` (`TARGET_NOT_APPLICATION_TARGET`).
3. An explicit DSN can never *select* an arbitrary database. There is no
   "verify some other database" mode.
4. **The configured application target alone is sufficient designation.** No
   second argument is required to proceed.
5. A missing, malformed or ambiguous application target is
   `BLOCKED — TARGET NOT VERIFIED` — never a pass, never an assumed default.
6. **Absence of a test DSN is never application-target proof.** The comparison
   simply does not apply; it does not vouch for anything.
7. An unreadable test DSN is a refusal (`TEST_TARGET_UNREADABLE`), not an excuse
   to skip the distinction — "the test target cannot be checked" and "the target
   is not the test database" are different claims.
8. The reached database must be proven independently. The connected database name
   must equal the declared `dbname`. If the driver reports a host, the reported
   host/port must normalize to the same identity; if it reports none, `host_compared`
   is recorded as `false` and no host claim is made.

---

## 3. Architecture (C4) — unchanged

* **Option A**: a dedicated, no-lock verification lifecycle.
* **No advisory locks** and no substitute coordination lock (`SELECT ... FOR
  UPDATE`, a sentinel row, a table lock, a file lock).
* **No `verify_schema_locked()`** and no reuse of the migration executor's locked
  entry point.
* **No modification of S1–S6.** `src.schema_contract`, `src.db_schema_catalog`,
  `src.physical_schema`, `src.db_migrate`, `src.startup_schema` are imported, not
  edited.
* **One connection, one transaction**: at most one connection attempt, no
  reconnect, no retry, exactly one `BEGIN ISOLATION LEVEL REPEATABLE READ READ
  ONLY`.
* **Same snapshot for G2 and G8**: catalog facts, ledger presence, ledger rows and
  server facts all come from that one transaction.
* **S2 and S3 are reused**: `read_public_catalog` produces the facts, and
  `evaluate_physical_schema` + `evaluate_migration_state` make every judgement.
  S8 owns no schema rule of its own.
* **Pinned assertions**: `MIGRATION_SET_DIGEST` and
  `CONTRACT_MIGRATION_VERSION` are asserted against the live ledger *and* against
  the shipped set.
* **PostgreSQL 12 minimum** (`server_version_num >= 120000`), read from the
  integer GUC — never from the human-readable `server_version` string.
* **No writes of any kind**; no migration, repair, cleanup or ledger creation.
* **Sanitized reporting**; no DSN, credential, hostname, role name, SQL text,
  driver message, employee row or biometric datum.
* **Separate authorization** required before any live execution.

---

## 4. Lifecycle ordering (C1)

Revision 2 ordered read-only enforcement before verification reads (§5) while
§6.2 read identity and version before L1–L3. Revision 3 fixes the order
explicitly. **No SQL verification read may precede the established read-only
transaction.**

| Step | Action | May issue SQL? |
|---|---|---|
| 1 | Authorization gate (`authorized` flag) | no |
| 2 | Resolve and normalize the application target and optional designated target | no |
| 3 | Validate the test-target distinction | no |
| 4 | Load the shipped migration set locally (filesystem) | no |
| 5 | Open **at most one** connection to the designated DSN | connect only |
| 6 | Read connection metadata that requires no SQL (`connection.info`) | no |
| 7 | Set `autocommit` as required | no |
| 8 | Request session read-only (L1) and verify the read-back (L2) | driver state only |
| 9 | Issue exactly one `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` | **one** statement |
| 10 | Confirm `transaction_read_only = on` and `transaction_isolation = repeatable read` (L3) | property reads only |
| 11 | Now execute the verification reads: connected-identity fallback, server version, catalog, ledger presence, ledger rows | reads |
| 12 | Capture all evidence on that same connection and transaction | reads |
| 13 | Roll back | transaction control |
| 14 | Close the connection | lifecycle |
| 15 | Evaluate the immutable captured snapshots | **no** — connection is closed |
| 16 | Determine the outcome and render the report | no |

Consequences:

* `connection.info.dbname` is read at step 6 because it needs no SQL. The
  `SELECT current_database()` **fallback** runs at step 11, after L3, and only
  when the driver exposes no database name. A missing identity is
  `TARGET_IDENTITY_UNREADABLE`, never a pass.
* The server version is read at step 11, not step 6. A pre-12 server is therefore
  refused *after* the read-only guarantees hold, which is the safe direction: S8
  still learns nothing about the schema before read-only is proven.
* Steps 13–14 happen **before** evaluation, so no judgement can depend on a live
  session, and no evaluation failure can leave a transaction open.

### 4.1 `_read_snapshot_statements()` reuse (C5)

S4's `_read_snapshot_statements()` builds statements that decide ledger presence
from `to_regclass` and then read `schema_version` rows. That is **not** the
rule this contract requires: presence must come from the **catalog**, so a ledger
the role cannot read is reported as unreadable rather than absent, and a
wrong-kind relation is reported as a schema defect rather than as a missing
ledger. Reuse would therefore change the answer, not just the code path.

Decision: **an S8-owned ledger adapter** (five fixed statements in
`src/s8/verification.py`), with S4 left unmodified. The trade-off is one small
duplicated statement set; the benefit is that S2/S3 remain the only schema
authorities and S4 keeps its locked-executor semantics untouched. The two
statements that S4 owns exclusively (`_BEGIN_SNAPSHOT_SQL`, `_LEDGER_PRESENT_SQL`)
are conceptually different here anyway, because S8's begin must carry `READ
ONLY` in its text.

---

## 5. Read-only guarantees

| Check | Guarantee |
|---|---|
| A9 | `connection.read_only = True` was accepted **and** reads back as `True`, before any statement |
| A10 | exactly one `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` was issued, verbatim |
| A11 | `transaction_read_only` reads `on` **and** `transaction_isolation` reads `repeatable read` |

Any failure in A9–A11 ends the run as
`BLOCKED — READ-ONLY GUARANTEE NOT ESTABLISHED`
(`READ_ONLY_SESSION_UNCONFIRMED`, `READ_ONLY_TRANSACTION_UNCONFIRMED`). No
verification read is issued, so no unproven read can influence a verdict.

---

## 6. Ledger evidence (C2)

Revision 2 filtered catalog presence to tables (E1) yet demanded detection of a
`schema_version` relation that exists but is not a table (D-6). Both cannot hold.
Revision 3 keeps the **relation-kind facts** and classifies before filtering.

Classification, from the S2 `CatalogSnapshot`:

| Catalog presence | `to_regclass` | Row read | Classification |
|---|---|---|---|
| no relation named `schema_version` | `null` | n/a | `absent` |
| relation(s), at least one ordinary/partitioned table | resolved | succeeded | `normal` |
| relation(s), at least one table | null | succeeded | `conflicting` → blocked |
| no relation | resolved | n/a | `conflicting` → blocked |
| relation(s), **none** a table (view, matview, sequence, foreign table, composite, index) | any | not attempted | `wrong-kind` |
| relation present | any | failed | `unreadable` → blocked |

Rules:

1. Presence is decided by the **catalog** (`pg_catalog` is world-readable), never
   by a privilege-sensitive probe alone.
2. `to_regclass` **corroborates** only. Disagreement with the catalog is
   incomplete evidence → `BLOCKED — DATABASE UNAVAILABLE`
   (`LEDGER_EVIDENCE_CONFLICTING`). It is never promoted to a definite failure.
3. A **wrong-kind** relation is a definite physical defect and is handed to the
   S3 evaluator, which reports `SCHEMA_TABLE_KIND_MISMATCH` →
   `FAIL — SCHEMA MISMATCH`. G2 is not evaluated against an unusable ledger.
4. An **absent** relation remains distinguishable from a wrong-kind relation:
   absent reaches G2 (`MIGRATION_LEDGER_MISSING` → `FAIL — MIGRATION HISTORY
   INVALID`).
5. An existing but unreadable ledger is `LEDGER_UNREADABLE` →
   `BLOCKED — DATABASE UNAVAILABLE`. Incomplete evidence is **never** converted
   into a definite migration-history failure.
6. Offline tests cover every case above, including the two directions in which
   the catalog and `to_regclass` disagree.

---

## 7. Outcomes (exactly seven, unchanged)

| Outcome | Exit | Meaning |
|---|---|---|
| `PASS — VERIFIED` | 0 | schema compatible **and** ledger consistent **and** pins matched |
| `FAIL — SCHEMA MISMATCH` | 6 | S3 reports a definite incompatibility |
| `FAIL — MIGRATION HISTORY INVALID` | 5 | G2 refuses the ledger |
| `BLOCKED — TARGET NOT VERIFIED` | 3 | identity/exclusivity could not be proven |
| `BLOCKED — AUTHORIZATION REQUIRED` | 2 | no explicit live authorization |
| `BLOCKED — DATABASE UNAVAILABLE` | 4 | could not read enough to decide |
| `BLOCKED — READ-ONLY GUARANTEE NOT ESTABLISHED` | 7 | A9–A11 failed |

Insufficient evidence maps to `BLOCKED — DATABASE UNAVAILABLE` plus a **reason
category** (`CONNECTION_FAILED`, `SERVER_VERSION_UNSUPPORTED`,
`SERVER_VERSION_UNREADABLE`, `CATALOG_UNREADABLE`, `LEDGER_UNREADABLE`,
`LEDGER_EVIDENCE_CONFLICTING`, `VERIFICATION_INCOMPLETE`, `CLEANUP_FAILED`).
Reason categories are additive detail; they never become new outcomes.

Ordering (documented precedence, first match wins):

1. A1–A5 refusals (authorization, target).
2. A6–A8 refusals (connection, identity).
3. A9–A11 refusals (read-only).
4. A12 (server version), A13 (shipped set), A14 (catalog), A15/A16 (ledger).
5. Test-marker detection → `BLOCKED — TARGET NOT VERIFIED`.
6. G2 → `FAIL — MIGRATION HISTORY INVALID`.
7. Pinned digest / contract version → `FAIL — MIGRATION HISTORY INVALID`.
8. S3 → `FAIL — SCHEMA MISMATCH`, or indeterminate → blocked.
9. Otherwise `PASS — VERIFIED`.

A ledger G2 refuses **is** a definite failure: the database is readable and
self-consistent, and the history genuinely disagrees with the shipped contract.

---

## 8. Evidence cases D-1 … D-7

| Case | Situation | Expected outcome |
|---|---|---|
| D-1 | ledger absent, catalog agrees | `FAIL — MIGRATION HISTORY INVALID` (`MIGRATION_LEDGER_MISSING`) |
| D-2 | ledger absent from catalog, `to_regclass` resolves | `BLOCKED — DATABASE UNAVAILABLE` (`LEDGER_EVIDENCE_CONFLICTING`) |
| D-3 | ordinary table, rows readable | continue to G2/G8 |
| D-4 | table present, `to_regclass` null, rows readable | `BLOCKED — DATABASE UNAVAILABLE` (`LEDGER_EVIDENCE_CONFLICTING`) |
| D-5 | table present, row read fails | `BLOCKED — DATABASE UNAVAILABLE` (`LEDGER_UNREADABLE`) |
| D-6 | relation present of a non-table kind | `FAIL — SCHEMA MISMATCH` (`SCHEMA_TABLE_KIND_MISMATCH`) |
| D-7 | catalog unreadable | `BLOCKED — DATABASE UNAVAILABLE` (`CATALOG_UNREADABLE`) |

Empty collections:

* **Empty shipped set** (`load_migrations()` returns nothing) →
  `FAIL — MIGRATION HISTORY INVALID` (`SHIPPED_MIGRATION_SET_EMPTY`) before any
  connection. A database cannot be verified against nothing.
* **Empty applied set with a present ledger** → G2's own diagnostic
  (`MIGRATIONS_PENDING`, or the ledger/inventory inconsistency code) →
  `FAIL — MIGRATION HISTORY INVALID`. S8 never calls `max()` on an empty
  sequence; the digest block records `evaluated: false` and
  `skipped_because: <code>`.
* **Empty applied set with an absent ledger** → D-1.

---

## 9. Pinned assertions

| Check | Rule | Failure |
|---|---|---|
| D1 (shipped) | `migration_set_digest(shipped)` equals `MIGRATION_SET_DIGEST` | `CONTRACT_PIN_INCONSISTENT` |
| D2 (live) | `migration_set_digest(applied)` equals `MIGRATION_SET_DIGEST` | `DIGEST_MISMATCH` |
| D3 (live version) | `max(applied version)` equals `CONTRACT_MIGRATION_VERSION` | `CONTRACT_VERSION_MISMATCH` |
| D4 (used set) | shipped max version equals `CONTRACT_MIGRATION_VERSION` | `CONTRACT_PIN_INCONSISTENT` |

All four are `FAIL — MIGRATION HISTORY INVALID`: the pinned contract is part of
the committed source, so a mismatch is a real disagreement, not a visibility
problem.

---

## 10. Connection accounting and cleanup

* `attempts` = 1 at the point of the single open attempt, `0` for every refusal
  that happens earlier.
* `opened` ∈ {0, 1}; never 2. There is no retry path, so accounting cannot drift.
* The connection is closed on every path, including `BaseException` (an
  interrupted run leaves nothing open).
* Cleanup precedence (`_apply_cleanup`, one rule):

| Primary outcome | Cleanup result | Reported outcome | `effect` |
|---|---|---|---|
| `PASS` | failed | `BLOCKED — DATABASE UNAVAILABLE` (+`CLEANUP_FAILED`) | `withheld-pass` |
| any `FAIL — …` | failed | unchanged (+`CLEANUP_FAILED`) | `retained-definite-failure` |
| any `BLOCKED — …` | failed | unchanged (+`CLEANUP_FAILED`) | `retained-definite-failure` |
| any | clean | unchanged | `none` |

A cleanup fault never manufactures a failure and never allows a pass.

---

## 11. Report

stdout only. No file is created implicitly; `--report <path>` refuses to
overwrite an existing file. Blocks, in this order: `tool`, `contract_version`,
`generated_at_utc`, `outcome`, `reason_categories`, `target`, `connection`,
`read_only`, `server`, `ledger`, `g2`, `digest`, `g8`, `checks`, `cleanup`,
`concurrency_limitation`.

Redaction is absolute: no DSN, no credential, no hostname (the declared host is
never echoed; only `host_compared` / `distinct_from_test_target` booleans), no
role name (`connection.role` is always `null`; no privilege probe is issued), no
SQL text, no driver message, no employee row, no biometric datum. `target.dbname`
is the one identifier reported, because an operator cannot act on a report that
does not say which database it describes.

Optional probes deliberately **omitted** in v1: privilege/ACL reads, a second
corroborating snapshot, and any quiescence requirement. Their absence is stated,
not hidden.

---

## 12. Mandatory offline test coverage

Every row has an expected result and is implemented in
`tests/test_s8_verification.py` (39 tests, all synthetic):

| # | Case | Expected |
|---|---|---|
| 1 | no authorization | `BLOCKED — AUTHORIZATION REQUIRED`, 0 attempts, no opener call |
| 2 | application target missing | `BLOCKED — TARGET NOT VERIFIED` (`…_UNCONFIGURED`) |
| 3 | malformed / ambiguous DSN (synthetic) | `BLOCKED — TARGET NOT VERIFIED` |
| 4 | explicit target selects another database | `BLOCKED — TARGET NOT VERIFIED` |
| 5 | explicit target restates the identity | proceeds |
| 6 | designated == test target | `BLOCKED — TARGET NOT VERIFIED` |
| 7 | unreadable test target / no test target | refusal / not proof |
| 8 | exactly one connection attempt, no reconnect | `attempts == 1`, `opened == 1` |
| 9 | connection failure | `BLOCKED — DATABASE UNAVAILABLE` |
| 10 | connected identity mismatch / unreadable | `BLOCKED — TARGET NOT VERIFIED` |
| 11 | reported host mismatch / host absent | refusal / `host_compared == false` |
| 12 | read-only not settable | `BLOCKED — READ-ONLY GUARANTEE NOT ESTABLISHED` |
| 13 | `transaction_read_only = off` | same, and no ledger read issued |
| 14 | isolation not repeatable read | same |
| 15 | BEGIN precedes every catalog read | ordering assertion on the statement log |
| 16 | server < 12 / unreadable / exactly 12 | blocked / blocked / proceeds |
| 17 | same-connection catalog + ledger reads | one connection, no second open |
| 18 | all ledger classifications (incl. wrong kind) | §6 table |
| 19 | empty shipped / empty applied | §8 |
| 20 | G2 precedence over G8 | migration outcome wins |
| 21 | test marker | `BLOCKED — TARGET NOT VERIFIED` |
| 22 | pinned digest / contract version | §9 |
| 23 | rollback failure / close failure | `CLEANUP_FAILED`, outcome per §10 |
| 24 | `BaseException` cleanup | connection still closed |
| 25 | sanitized JSON and human report | no secrets, limitation present |
| 26 | no advisory-lock SQL, no forbidden methods | statement log has none |

---

## 13. Acceptance review (C6)

| Item | Result |
|---|---|
| Cross-references (§2.1 / §2.2, §5 / §6.2, E1 / D-6) | **resolved** — §2, §4, §6 state one rule each |
| Lifecycle ordering | **consistent** — §4 table, verified by test 15 |
| Outcome mappings | **complete** — §7, all seven, with exit codes |
| D-1 … D-7 evidence cases | **complete** — §8, one expected outcome each |
| Empty shipped / applied collections | **complete** — §8 |
| Connection accounting | **complete** — §10 |
| Cleanup precedence | **complete** — §10, single rule |
| Report redaction | **complete** — §11 |
| No-repair policy | **complete** — no INSERT/UPDATE/DELETE/DDL anywhere |
| Every mandatory test has an expected result | **yes** — §12 |

### Remaining unresolved decisions

1. **Exit codes.** S8 owns 0,2,3,4,5,6,7. `tools/run_migrations.py` already uses
   2 for config/target errors, which matches S8's 2 and 3 only by coincidence.
   Keeping S8's mapping is deliberate (S8 is a distinct tool), but if the owner
   prefers one project-wide mapping this table must change before live use.
2. **`host_compared` when the driver reports no host.** Recorded as `false`
   rather than refusing: a driver that does not expose the host cannot be used to
   prove a mismatch, and refusing would make unix-socket connections
   unverifiable. Live behaviour on a socket connection is **NOT VERIFIED**.
3. **Partitioned `schema_version`.** `is_table` includes partitioned tables, so
   such a ledger is `normal` for presence and then refused by S3 as a kind
   mismatch. This is intentional, but it means "wrong kind" is decided by S3, not
   by the classifier.

None of these blocks offline implementation. All live behaviour remains
**NOT VERIFIED — REQUIRES SEPARATE AUTHORIZATION**.
