# P5.5 — Audit Journal: Design and Decision Record (P5.5.1)

**Status: design revision complete; awaits owner review. No P5.5 code exists and
P5.5.2 has not started.** This document supersedes the first P5.5.1 design
summary (which lived only in the review report) and resolves the independent
engineering review findings.

Scope: a separate, append-only, hash-chained audit journal for security events.
Not an audit database table, not an external logging service, not biometric
storage, and not a change to employee or attendance semantics.

---

## 1. Confirmed architecture

* Journal separate from the attendance JSONL journal.
* Active journal: `data/audit/audit.jsonl`.
* Append-only JSON Lines, one JSON object per line, UTF-8, `\n` terminated.
* SHA-256 over canonical JSON; per-record digest plus previous-hash link.
* Every append is `write` → `flush` → `os.fsync` before it is treated as
  durable.
* No automatic repair, truncation or rewriting of torn or corrupt records.
* Employee and attendance mutations are **mandatory, fail-closed** audited.
* Login, logout and permission-denial events are **best-effort**.
* Verification CLI is read-only.
* Retention is **at least 365 days**, initially **archive-only**; there is no
  automatic deletion and no deletion schedule in P5.5.
* Rotation is **operator-controlled**, never automatic at startup.
* No new database table, Redis, external logging service, or biometric data.
* Timestamps are application-generated UTC from the existing clock seam.
* Actor identity comes only from the validated `AuthenticatedPrincipal`.
* Tamper-evident, **not** tamper-proof: an attacker who can rewrite the whole
  journal and manifest can rebuild every digest. No document or CLI output may
  claim otherwise.

---

## 2. Record schema (event journal)

One line per record, exactly these top-level fields; unknown or missing fields
are invalid.

| Field | Type | Nullable | Rule |
|---|---|---|---|
| `schema_version` | int | no | `1` for P5.5 |
| `sequence` | int | no | **Global** 1-based sequence, never reset (see §4) |
| `segment_id` | string | no | Segment identifier, `YYYY-MM` (UTC) |
| `segment_sequence` | int | no | 1-based position **within** the segment |
| `audit_id` | string (UUIDv4) | no | Stable record identity |
| `timestamp` | string | no | UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ` |
| `actor_user_id` | string | **yes** | `null` iff `actor_role` is `null` (§2.2) |
| `actor_role` | string | **yes** | `Admin` \| `HR Manager` \| `Operator` \| `Auditor` |
| `action` | string | no | Closed vocabulary (§2.3) |
| `target_type` | string | **yes** | `employee` \| `attendance_event` \| `account` \| `session` \| `endpoint` |
| `target_id` | string | **yes** | Opaque identifier only; ≤ 256 chars; never a name, hash, token or free text |
| `outcome` | string | no | `success` \| `denied` \| `error` \| `uncertain` |
| `reason_code` | string | no | Closed vocabulary (§2.4) |
| `correlation_id` | string (UUIDv4) | **yes** | Result records: the intent's `audit_id`; standalone events: `null` |
| `request_id` | string (UUIDv4) | **yes** | Server-generated request ID; `null` outside an HTTP request |
| `source` | string | no | `api` \| `cli` \| `system` (no other values; no `test`) |
| `metadata` | object | no | Allowlisted scalar keys only, `{}` when empty (§2.5) |
| `previous_hash` | string | no | Lowercase 64-hex SHA-256; genesis per §4 |
| `record_hash` | string | no | Lowercase 64-hex SHA-256 of the canonical body |

Size bounds (enforced at construction, before any write): serialized record
≤ 8192 bytes; `metadata` ≤ 16 keys and ≤ 1024 bytes serialized; `target_id`
≤ 256 chars; strings must be valid UTF-8 without lone surrogates.

### 2.1 Binding rules

* `schema_version`, `sequence`, `segment_id`, `segment_sequence`, `audit_id`,
  `timestamp`, `action`, `outcome`, `reason_code`, `source`, `previous_hash`
  and `record_hash` are always present and non-null.
* `actor_user_id`/`actor_role` are both `null` (pre-authentication/system) or
  both set. A null actor is only valid for `auth.login`/`auth.logout` with a
  pre-session outcome, and always with a reason code that does not describe an
  authenticated actor's action.
* `correlation_id` equals the intent record's `audit_id` for result records and
  is `null` for standalone events (`auth.login`, `auth.logout`, `authz.denied`).
* `target_type`/`target_id` are both `null` or both set. Login failures use
  `target_type="account"` and **`target_id=null`** (no username key retained).
* `metadata` values must be `str`, `int` or `bool`; no floats, no nested
  objects, no arrays.

### 2.2 Actor model

`AuthenticatedPrincipal` supplies `user_id` and `role`; nothing else is read
from the request. Unauthenticated events (failed login, rate-limited login,
malformed credentials) are system events with a null actor. There is no
"system user" pseudo-identity with a grantable role.

### 2.3 Closed action vocabulary

`employee.create`, `employee.update`, `employee.deactivate`,
`attendance.checkout`, `auth.login`, `auth.logout`, `authz.denied`.

Deliberately absent: `audit.verify` (the verifier is read-only and writes no
events) and `audit.rotate` (rotation evidence lives in the **manifest**, not the
event journal — see §9). Callers cannot supply action names; the enum refuses
unknown values.

### 2.4 Closed reason-code vocabulary

`OK`, `AWAITING_RESULT`, `EMPLOYEE_EXISTS`, `EMPLOYEE_NOT_FOUND`,
`EMPLOYEE_INVALID`, `EMPLOYEE_INACTIVE`, `NO_OPEN_SESSION`, `CHECKOUT_COOLDOWN`,
`PERSISTENCE_UNCONFIRMED`, `JOURNAL_CORRUPT`, `INVALID_CREDENTIALS`,
`ACCOUNT_INACTIVE`, `RATE_LIMITED`, `SESSION_ABSENT`, `SESSION_INVALID`,
`PERMISSION_DENIED`, `DATABASE_UNAVAILABLE`, `DATABASE_OUTCOME_UNKNOWN`,
`INTERNAL_ERROR`, `VALIDATION_FAILED`.

`AUDIT_WRITE_FAILED` is deliberately **not** a reason code: a failed audit write
cannot be recorded in the journal that just failed. Such failures are surfaced
as structured operational logs only (§8).

### 2.5 Metadata allowlist

Only these keys may appear, with scalar values:

`endpoint` (route template such as `/api/v1/employees/{employee_id}`, never a
raw URL, never a query string), `method`, `http_status`,
`employee_active_after`, `journal_event_id`, `prior_role`, `new_role`.

Request bodies, query strings, headers, cookies, file paths, DSNs, usernames,
names and biometric values are never valid metadata. `endpoint` is a route
template so an attacker cannot inject a path into the audit trail.

### 2.6 Example (result record)

```json
{"schema_version":1,"sequence":42,"segment_id":"2026-11","segment_sequence":42,
 "audit_id":"6a0e…","timestamp":"2026-11-14T11:02:33.123456Z",
 "actor_user_id":"user-1","actor_role":"Admin","action":"employee.deactivate",
 "target_type":"employee","target_id":"E1","outcome":"success","reason_code":"OK",
 "correlation_id":"c6f1…","request_id":"0b1d…","source":"api",
 "metadata":{"endpoint":"/api/v1/employees/{employee_id}/deactivate","method":"POST","http_status":200},
 "previous_hash":"<64 hex>","record_hash":"<64 hex>"}
```

---

## 3. Canonical serialization and hashing

* `record_hash = sha256(canonical_body_bytes).hexdigest()` where
  `canonical_body_bytes = json.dumps(body, ensure_ascii=False, sort_keys=True,
  separators=(",", ":"), allow_nan=False).encode("utf-8")` and `body` is every
  field except `record_hash`.
* Timestamps are normalized **before** serialization: timezone-aware UTC,
  microseconds always present, `%Y-%m-%dT%H:%M:%S.%06dZ`. `isoformat()` output
  (`+00:00`) is never stored, so one logical record has exactly one digest.
* Integers only for `schema_version`, `sequence`, `segment_sequence`; metadata
  values are int/str/bool.
* Verification re-serializes the parsed body canonically; formatting
  differences in the file (key order, whitespace) do not change the digest, but
  any change to a field value, type or nullability does.
* Digests are compared with `hmac.compare_digest`.
* A record with an unknown `schema_version` is invalid — it is never parsed
  "best effort".

---

## 4. Chain, sequence and genesis

* Genesis: the first-ever audit record has `previous_hash = "0" * 64`.
* Every later record's `previous_hash` equals the immediately preceding
  record's `record_hash` — **including across segment boundaries**. Segment
  rotation never resets the chain to zeros.
* `sequence` is **global**: the first record is `1` and each later record is
  `previous.sequence + 1`, across all segments, forever.
* `segment_sequence` is segment-local: the first record of each segment is `1`
  and each later record in that segment increments by one.
* `segment_id` is the UTC calendar month (`YYYY-MM`) and is unique across
  sealed segments; a segment cannot be reopened once sealed.
* Verifier continuity rules:
  1. within a segment: `segment_sequence` rises by exactly 1, and global
     `sequence` rises by exactly 1;
  2. at a segment boundary: the first record's global `sequence` equals the last
     record of the preceding segment + 1, and its `previous_hash` equals that
     record's `record_hash`;
  3. the first available segment's first record is either the true genesis
     (`"0"*64`, `sequence == 1`) or an anchor whose predecessor is declared
     unavailable by a `retention_decision` manifest entry (§9) — in the latter
     case the run is reported as anchor-only coverage, never full-chain.
* There are **no trusted checkpoints** in P5.5: none may be created, and the
  writer has no shortcut that skips verification (§5).

---

## 5. Writer trust, poisoning and durability

* **Full verification before the first write.** A writer construction reads the
  active journal **and** the manifest **and** every declared sealed segment,
  verifies the entire available chain (§4, §9.4) and only then accepts writes.
  It never trusts the tail record's hash alone, never trusts a cached last hash
  from a previous run, and never trusts a file's size. A file-size or serial
  mismatch is used only as a change detector that makes the writer refuse; it is
  never evidence of integrity.
* If verification fails, the writer is not created; the application enters
  restricted mode (§7) and no appends occur.
* **Process-local `threading.Lock`** serializes append steps inside the process
  (sync routes run in a thread pool). An OS advisory lock on the journal
  (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) is held for the append as
  defense-in-depth against an accidental second process; this is **not**
  multi-process support and is documented as best-effort.
* **Single writer state machine**: `initializing → ready → poisoned`.
  * Append steps are ordered: build body (sequence = last+1, previous_hash =
    last hash) → canonicalize → digest → `write(line + "\n")` → `flush()` →
    `os.fsync(fd)` → update cached last sequence/hash → return.
  * Only a successful `fsync` advances the cache.
  * **Any ambiguous failure poisons the writer**: a raised `OSError` from
    `write`, `flush`, `fsync`, a partial write, a short write, an interrupted
    call, or a file-size/serial mismatch. A failed write does **not** prove no
    bytes reached disk.
  * A poisoned writer refuses every subsequent append with
    `AuditJournalPoisonedError`. It never retries, never truncates, never
    repairs, and never "reopens" the file.
  * Recovery is explicit: stop the process, run the read-only verifier, resolve
    the tail by operator procedure, then start a fresh writer (which performs
    full verification again). There is no automatic recovery path.
* Typed errors (all `AuditJournalError` subclasses; messages are safe for
  server logs, never for clients):
  `AuditJournalIntegrityError` (verification failed, carries line/position),
  `AuditJournalCorruptError` (torn/malformed tail or schema violation),
  `AuditJournalUnavailableError` (directory/file/permission/disk I/O),
  `AuditJournalPoisonedError` (instance poisoned), and
  `AuditJournalContendedError` (external writer detected).
* Diagnostics: server-side structured log with the error class and the journal
  path only; client responses carry a stable code and a fixed sentence, never a
  path, exception message or stack trace.
* No directory `fsync` is claimed as a guarantee: file `fsync` is the durability
  boundary for record contents, and directory-entry durability (segment rename)
  is explicitly best-effort and re-verified by the manifest (§9).

---

## 6. Crash and corruption behavior

| Scenario | Journal state | Behaviour |
|---|---|---|
| Crash before append | unchanged | No record; for a mutation this is "intent not written", so the mutation was never attempted |
| Crash during append | torn tail (no final newline) | Writer refuses to start (restricted mode); verifier reports integrity failure at the tail; nothing is repaired |
| Crash after append, before response | record durable | The record is the truth; a client retry creates a second, correlated record |
| Crash between intent and result | intent `outcome=uncertain`, `reason_code=AWAITING_RESULT` | Pending, not success; result may never exist |
| Corrupt middle record | chain broken | Full verification fails; writer refuses; verifier names the line |
| Corrupt manifest / missing segment | archive inconsistency | Verifier exit 4; writer refuses (restricted mode) |
| Duplicate sequence or reordered records | chain/sequence invalid | Verification failure; never "fixed" |
| Disk full / permission denied | append raises | Writer poisons; mutation fails closed (§7, §10) |
| Journal path is a directory or unreadable | unavailable | Writer refuses; restricted mode |

Normal operation never truncates, rewrites or repairs. Remediation is an
operator procedure documented in P5.5.7 (inspect, move aside, re-verify).

---

## 7. Startup, missing journal and restricted mode

* **Safe initialization** is allowed only when `data/audit/` contains no
  journal, no manifest and no archive files (absent or empty directory): the
  writer creates `audit.jsonl` and the manifest, both starting at genesis.
* In every other case the writer performs full verification (§5). Success →
  `ready`. Any failure → **restricted mode**:
  * the four audited mutation routes answer `503 AUDIT_UNAVAILABLE` and perform
    no business operation;
  * reads (`GET` employees/attendance), `/health`, login, logout and `/auth/me`
    keep working; best-effort events are skipped and surfaced in logs;
  * one critical structured log line records the failure class and path;
  * `/health` keeps its existing liveness contract (process is up) and does not
    become an audit-readiness claim; P5.5 does not change its schema.
* An optional strict mode (`AUDIT_REQUIRE_INTEGRITY`, staging/production) may
  refuse to start at all instead of running restricted; that is an owner
  decision (§13) and is not implemented in P5.5.2–P5.5.6.
* P5.5.1 makes no claim that startup verification exists today; it is a P5.5.3
  requirement.

---

## 8. Best-effort events and failure surfacing

For `auth.login`, `auth.logout` and `authz.denied`:

* The audit write is attempted but never changes the HTTP response, status code,
  headers or cookies. Login throttling, origin checks, session behavior and
  authorization decisions are untouched.
* A failure produces one structured `WARNING` log carrying: the action, the
  request ID (when present), and a safe error classification
  (`AuditJournalUnavailableError`, `AuditJournalPoisonedError`,
  `AuditJournalCorruptError`, `AuditJournalIntegrityError`). No username, no
  actor identity for failures, no password/hash/token/cookie/DSN value, no
  metadata content.
* A failed best-effort write cannot record `AUDIT_WRITE_FAILED` in the journal
  that failed; it is observable only through logs. Operational alerting on that
  log line is the documented mechanism.
* Record size bounds (§2) keep a disk-fill attempt from writing arbitrarily
  large lines; login attempts are already bounded by the existing 5/15-minute
  throttle. **No new throttling is added** and the approved throttle policy is
  unchanged.
* Login failure records use `target_type="account"`, `target_id=null`: the
  normalized username is deliberately not retained.

---

## 9. Retention, rotation and the manifest

Policy: **retain at least 365 days; initial implementation is archive-only; no
automatic deletion and no deletion schedule.** Rotation is operator-controlled
(target cadence: monthly) and runs while the application is stopped, or under
the writer's exclusive lock with the writer disabled; rotation is implemented in
**P5.5.7**, not P5.5.3.

### 9.1 Layout

```
data/audit/
  audit.jsonl                       active segment (append-only, chained)
  archive/audit-<first>-<last>.jsonl sealed segments (immutable, chained)
  manifest.jsonl                    append-only segment index (own chain)
  .rotation.lock                    operator rotation lock (no secrets)
```

### 9.2 Manifest schema (distinct from the event schema)

One line per entry: `manifest_version` (1), `manifest_sequence` (global, 1-based),
`entry_type` (`segment_sealed` | `retention_decision`), `segment_id`,
`disposition` (`archived` | `archived_offsite`), `segment_file` (relative path
under `data/audit/`, `null` for an offsite disposition), `first_global_sequence`,
`last_global_sequence`, `record_count`, `first_previous_hash`,
`last_record_hash`, `sealed_at` (same fixed UTC format), `tool`,
`tool_version`, `previous_manifest_hash`, `entry_hash`. Unknown or missing
manifest fields are invalid, exactly like event records.

`entry_hash = sha256(canonical_json(body without entry_hash))`, chained by
`previous_manifest_hash` with the same `"0"*64` genesis. Sealed-segment entries
are unique per `segment_id`; a duplicate is an integrity failure. Retention
entries record `segment_id` and the disposition (`archived_offsite`) plus the
record count — never a claim that contents were verified after removal.

### 9.3 Rotation steps (operator tool, P5.5.7)

1. Stop the application (or take the exclusive writer lock with the writer
   disabled); verify the full available chain; ensure the active file ends with
   a newline.
2. `os.replace(active, archive/audit-<first>-<last>.jsonl)`; `fsync` the
   directory (best effort, verified by step 3/4 and by the verifier).
3. Append the `segment_sealed` manifest entry with the segment's first/last
   global sequence, record count, `first_previous_hash`, `last_record_hash` and
   fsync; mark the archive file read-only where the OS allows.
4. Create the new empty active journal; its first record links to the sealed
   segment's `last_record_hash` and continues the global sequence. No
   `audit.rotate` event is written to the event journal.

### 9.4 Rotation crash recovery

| Crash point | Saved state | Required behaviour |
|---|---|---|
| During step 1 | Nothing changed | Re-run rotation; no journal change |
| After rename, before manifest entry | Archive present, manifest lacks the segment | Verifier exits 4; the tool offers an explicit `--register-existing` that re-verifies the archive's full chain before appending the manifest entry. Never silent; never a second seal |
| After manifest entry, before new active file | Manifest declares the segment, active file absent | Writer/verifier treat as "segment sealed, active missing": a new active file may be created linked to the manifest's `last_record_hash` (defined, non-destructive recovery) |
| After new active file, before first record | Empty active file | Normal: first append uses the manifest's last hash as `previous_hash` |
| Crash mid-manifest append | Torn manifest tail | Verifier exits 4; writer refuses; operator resolves with the verifier — no automatic repair |

A crash may never produce silent loss, duplicate acceptance of one segment, or a
chain reset; an inconsistent state is detected and reported, never "fixed".

### 9.5 Verification coverage (three distinct modes)

1. **Full historical record-chain verification** — every record of every
   available segment, all digests, sequences and links (including boundaries).
2. **Segment/manifest continuity** — manifest chain intact, each declared
   segment present, counts and hash anchors matching the segment contents.
3. **Anchor continuity only** — when a segment's contents are no longer
   available after an owner-approved retention disposition: the manifest anchor
   is verified against its own chain and the surrounding link, and the run is
   reported as **anchor-only coverage**. This is never described as proof that
   the removed records were verified.

The CLI always states which modes applied and which parts of history were not
covered by content verification.

---

## 10. Mutation audit protocol and HTTP contract

Applies to `employee.create`, `employee.update`, `employee.deactivate`,
`attendance.checkout` (and, from P5.6, user/role changes).

1. **Intent**: build and durably append `outcome=uncertain`,
   `reason_code=AWAITING_RESULT`, with actor, action and target. If this write
   fails or the writer is not `ready`, the mutation is **not executed** and the
   response is `503 AUDIT_UNAVAILABLE`.
2. **Execute** the unchanged business operation.
3. **Classify the outcome** into exactly one of:
   * **Confirmed applied** — success was returned and (for attendance) the
     durable append returned.
   * **Confirmed not applied** — evidence proves no effect: request validation,
     business refusal (`CheckoutRejected`, `EmployeeValidationError`), a unique
     constraint rejection during `INSERT`, or a connection that could not be
     opened before any statement ran.
   * **Unknown** — everything else: commit failure, connection loss or timeout
     after a statement was sent, process interruption, or any error whose
     post-state cannot be established. **A database exception is not assumed to
     mean rollback.**
4. **Result** record with `correlation_id` = intent `audit_id`:
   * applied → `outcome=success`, `reason_code=OK`;
   * not applied → `outcome=error`, the matching reason code;
   * unknown → `outcome=uncertain`, `reason_code=DATABASE_OUTCOME_UNKNOWN`
     (or `PERSISTENCE_UNCONFIRMED` for the attendance journal).
5. **Result-write failure**: if the result cannot be recorded after step 2 may
   have succeeded, respond `503 AUDIT_RESULT_UNCERTAIN` — the operation may have
   applied and is not fully audited. Never a 2xx. No retry, no compensating
   write.
6. If the mutation itself failed and the result write also failed, the original
   business error response stands (the state is known unchanged) and the audit
   gap is surfaced in logs.

An intent record proves only that an attempt was started. It is never treated as
success, and a pending intent is reported as unknown.

### 10.1 HTTP contract

| Condition | Status | Code | Fixed message |
|---|---|---|---|
| Writer not ready/poisoned, or intent write failed | 503 | `AUDIT_UNAVAILABLE` | "The operation was not attempted because it could not be audited." |
| Result write failed after the mutation may have applied | 503 | `AUDIT_RESULT_UNCERTAIN` | "The operation may have been applied, but its audit result could not be recorded. Do not retry blindly." |
| Mutation outcome unknown, result record written (`outcome=uncertain`) | 503 | `DATABASE_UNAVAILABLE` (existing mapping) | existing sanitized message; a 503 here does **not** prove the mutation failed |
| Best-effort event write failed (login/logout/denied) | unchanged | unchanged | unchanged |

Both audit 503s are sanitized: no exception text, path, stack trace, SQL, token
or username. A 503 under `AUDIT_RESULT_UNCERTAIN` explicitly does not mean the
business operation failed.

---

## 11. Verification CLI (`tools/audit_verify.py`)

Read-only. Never writes, repairs, rotates, rewrites or locks for writing.

```
python tools/audit_verify.py [--journal PATH] [--archive-dir PATH]
                             [--manifest PATH] [--json] [--max-errors N]
```

Checks: UTF-8 and JSON syntax per line; supported `schema_version`; required and
unknown fields; nullability and vocabulary rules; metadata allowlist and size
bounds; global and segment sequence continuity; genesis; previous-hash links
including segment boundaries; recomputed `record_hash`; torn or malformed
records; manifest schema, chain and entry uniqueness; segment/manifest agreement
and cross-segment links; coverage mode (full-chain, segment/manifest continuity,
anchor-only).

Exit codes: `0` verified (with the coverage mode printed); `2` integrity failure
(hash, sequence, link, schema, torn tail); `3` invalid arguments; `4`
archive/manifest inconsistency (missing/unexpected segment, count or anchor
mismatch, torn manifest).

Output: coverage summary, record/segment counts, first failure position and
reason; `--json` for machine consumption. It reports identifiers needed to
locate a failure and never dumps record bodies, metadata values, paths beyond
the configured journal root, or any user data beyond a `target_id` when that id
is the failing record's key.

---

## 12. Test matrix (planned)

| Area | Cases | Expected |
|---|---|---|
| Schema | missing/unknown fields, wrong types, null-actor rules, unknown action/reason/outcome/source, oversized record/metadata, non-scalar metadata | typed validation error, nothing written |
| Canonicalization | key order, whitespace, `ensure_ascii`, timestamp normalization, int sequences | identical digest; any value change changes it |
| Chain/sequence | genesis, segment boundary link, global vs segment sequence, gap, duplicate, reorder, forged digest | valid chain passes; every defect fails with position |
| Writer trust | existing journal, dirty tail, corrupt middle, manifest mismatch, missing archive | construction refused; restricted mode; no append |
| Poisoning | partial write, short write, `os.fsync` failure, `flush` failure, size mismatch | writer poisoned, no cache advance, all later appends refused, restart requires full verification |
| Concurrency | 20 threads × 50 appends, external writer, OS lock conflict | contiguous unique sequences, valid chain, contention refused |
| Crash | torn tail, crash between intent/result, crash at each rotation stage | explicit integrity/consistency failures, no repair, no duplicate seal, no chain reset |
| Disk/permission | read-only dir, disk-full simulation, unreadable file | typed unavailable error, poison, fail-closed mutation |
| Mutation protocol | intent fail, result fail, applied, not-applied, unknown, both fail | no mutation; 503 `AUDIT_UNAVAILABLE`; 503 `AUDIT_RESULT_UNCERTAIN`; correct result outcomes |
| Best-effort | login success/failure/429, logout, 403 write failures | response unchanged, warning logged, no identity or secret logged |
| Propagation | actor/role, request ID, correlation IDs, `source` | exact values; nulls only where allowed |
| Exclusion | password/hash/token/cookie/DSN/embedding/body in any field | rejected or absent; sweep finds nothing |
| Rotation/retention | seal/rename/manifest/next-active, missing segment with retention entry, offsite disposition | chain continuity preserved; coverage reported honestly; exit 4 on inconsistency |
| CLI | healthy, tampered, torn, unsupported version, bad args, `--json` | exit 0/2/2/2/3/0; no file modification |

All tests use temporary directories, injected clocks and monkeypatched I/O; no
PostgreSQL and no biometric data are required.

---

## 13. P5.5 sub-milestones

| # | Scope | Prerequisites | Exclusions | Verification | Exit criteria |
|---|---|---|---|---|---|
| **P5.5.1** | This design: schema, vocabulary, hash/chain rules, writer trust/poisoning, mutation protocol, startup policy, retention/rotation, CLI contract, test matrix | P5.4.10 accepted | Any code | Owner review of consistency | Owner approves; no open contradiction |
| **P5.5.2** | Schema module: field types/nullability, vocabularies, metadata allowlist, size bounds, canonical serialization, SHA-256, genesis, record validation errors | P5.5.1 | Writer, filesystem, HTTP | ~70 unit tests (schema, canonicalization, digests, tamper at record level) | All matrix rows for schema/hash pass; no I/O |
| **P5.5.3** | Durable writer: initialization, full verification before first write, process + OS lock, append/flush/fsync, poisoning, typed errors, restricted-mode signal; **no rotation** | P5.5.2 | Rotation, manifest writes, HTTP wiring | Concurrency, poisoning, verification-before-write, crash/torn tests | Writer never appends to an unverified journal; ambiguous failures poison; rotation absent |
| **P5.5.4** | `AuditService`: actor/request-ID binding, action/reason API, intent/result helpers, best-effort wrapper, safe logging | P5.5.3 | Route behavior changes | Unit tests for actor/request propagation, intent/result pairing, logging redaction | Service can emit every event type; no secret in any output |
| **P5.5.5** | Route integration: mandatory intent→mutate→result for the four mutations, best-effort login/logout/403, `AUDIT_UNAVAILABLE`/`AUDIT_RESULT_UNCERTAIN` handlers, startup restricted mode | P5.5.4 | User admin, frontend, new endpoints | Endpoint tests for applied/not-applied/unknown, fail-closed, best-effort | No unaudited success possible; existing auth/authz behavior unchanged |
| **P5.5.6** | Read-only verifier CLI with coverage modes and exit codes | P5.5.3 | Rotation, deletion | CLI tests: tamper matrix, coverage reporting, bytes unchanged | Exit 0/2/3/4 correct; never writes |
| **P5.5.7** | Rotation, manifest, archives, retention documentation, operator runbook; monthly cadence | P5.5.6 | Automatic deletion/schedule | Four crash-stage tests, manifest continuity, immutability, retention entries | Rotation preserves chain; crash states detected; no silent mutation of history |
| **P5.5.8** | Security regression, fault injection, exclusion sweep, docs and acceptance | P5.5.1–P5.5.7 | P5.6 work | Full matrix + soak + review | All criteria met; owner acceptance |

Rotation is deliberately **P5.5.7**; P5.5.3 implements only the single active
journal.

---

## 14. Residual decisions for the owner

1. Login-failure records keep `target_type="account"`, `target_id=null` (no
   username); confirm this data-minimisation choice.
2. Startup on a corrupt journal: restricted mode (recommended, defined in §7)
   versus refuse-to-start; and whether `AUDIT_REQUIRE_INTEGRITY` is on by default
   in staging/production.
3. Whether `authz.denied` metadata may carry the route template and method
   (recommended) or only the HTTP status.
4. Retention disposition wording for offsite archives (P5.5.7) and the operator
   procedure for 365-day archive movement; no deletion is authorised here.
5. Whether the absence of a declared segment (moved offsite) should ever permit
   writing in anchor-only mode; the design currently **refuses** until an owner
   decision defines it.
6. Confirmation that the two new error codes (`AUDIT_UNAVAILABLE`,
   `AUDIT_RESULT_UNCERTAIN`) and their fixed messages are approved.
