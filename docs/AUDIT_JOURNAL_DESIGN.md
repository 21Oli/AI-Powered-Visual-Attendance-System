# P5.5 — Audit Journal: Design and Decision Record (P5.5.1)

**Status: P5.5.1 design approved by the project owner. P5.5.2 (audit event
schema, canonical serialization, SHA-256 record hashing and hash-chain
verification primitives) is implemented in `src/audit_schema.py` with focused
unit tests, and awaits independent review. The owner-approved `YYYY-MM-NNN`
segment-identity correction is applied (§16). Nothing else exists: there is no
writer, journal file, manifest, API integration, verifier CLI or rotation.**
This document supersedes the first P5.5.1 design summary (which lived only in
the review report), resolves the independent engineering review findings and
records the owner's decisions (§14) plus the P5.5.2 implementation notes (§15).

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
| `segment_id` | string | no | `YYYY-MM-NNN` — UTC **allocation** month plus a `001`–`999` ordinal (§4, §16); unique across all segments; never an ordering claim |
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
* `segment_id` is `YYYY-MM-NNN`: the UTC month in which the segment was
  **allocated**, plus a `001`–`999` allocation ordinal (e.g. `2026-11-001`,
  `2026-11-002`, `2026-12-001`). Ordinal `000` and four-digit ordinals are
  invalid. The identifier is unique across all segments (current and archived);
  a segment cannot be reopened once sealed. Any number of segments may be
  allocated in one calendar month — "monthly" is a rotation cadence target, not
  a uniqueness guarantee (§16).
* Segment order is established by global sequence continuity, segment-local
  restart at 1, and the predecessor `record_hash` link — **never** by comparing
  segment labels. The verifier performs no lexical ordering check; manifest
  seal order is added in P5.5.7.
* A record's `timestamp` is independent of its segment's allocation month: the
  schema validates the fixed UTC form only and never requires month equality
  (§16.5).
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
4. Create the new empty active journal under a fresh `YYYY-MM-NNN` id (the next
   ordinal for its allocation month, `001` when the month has none); its first
   record links to the sealed segment's `last_record_hash` and continues the
   global sequence. No `audit.rotate` event is written to the event journal.

### 9.4 Rotation crash recovery

| Crash point | Saved state | Required behaviour |
|---|---|---|
| During step 1 | Nothing changed | Re-run rotation; no journal change |
| After rename, before manifest entry | Archive present, manifest lacks the segment | Verifier exits 4; the tool offers an explicit `--register-existing` that re-verifies the archive's full chain before appending the manifest entry. Never silent; never a second seal |
| After manifest entry, before new active file | Manifest declares the segment, active file absent | Writer/verifier treat as "segment sealed, active missing": a new active file may be created with a fresh `YYYY-MM-NNN` id, linked to the manifest's `last_record_hash` (defined, non-destructive recovery) |
| After new active file, before first record | Empty active file | Normal: the id is derived deterministically from the sealed predecessor (same allocation month, ordinal + 1) so restarts agree; first append uses the manifest's last hash as `previous_hash` |
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
| Mutation outcome unknown, result record written (`outcome=uncertain`, `reason_code=DATABASE_OUTCOME_UNKNOWN`) | 503 | `DATABASE_UNAVAILABLE` (existing status/code mapping) | "The operation's outcome could not be confirmed; it may or may not have been applied." — the message must state the uncertainty and must never imply definite failure |
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
| **P5.5.2** | Schema module: field types/nullability, vocabularies, metadata allowlist, size bounds, canonical serialization, SHA-256, genesis, record validation errors, `YYYY-MM-NNN` segment identity | P5.5.1 | Writer, filesystem, HTTP | 172 unit tests (schema, canonicalization, digests, tamper at record level) | All matrix rows for schema/hash pass; no I/O |
| **P5.5.3** | Durable writer: initialization, full verification before first write, process + OS lock, append/flush/fsync, poisoning, typed errors, restricted-mode signal; **no rotation** | P5.5.2 | Rotation, manifest writes, HTTP wiring | Concurrency, poisoning, verification-before-write, crash/torn tests | Writer never appends to an unverified journal; ambiguous failures poison; rotation absent |
| **P5.5.4** | `AuditService`: actor/request-ID binding, action/reason API, intent/result helpers, best-effort wrapper, safe logging | P5.5.3 | Route behavior changes | Unit tests for actor/request propagation, intent/result pairing, logging redaction | Service can emit every event type; no secret in any output |
| **P5.5.5** | Route integration: mandatory intent→mutate→result for the four mutations, best-effort login/logout/403, `AUDIT_UNAVAILABLE`/`AUDIT_RESULT_UNCERTAIN` handlers, startup restricted mode | P5.5.4 | User admin, frontend, new endpoints | Endpoint tests for applied/not-applied/unknown, fail-closed, best-effort | No unaudited success possible; existing auth/authz behavior unchanged |
| **P5.5.6** | Read-only verifier CLI with coverage modes and exit codes | P5.5.3 | Rotation, deletion | CLI tests: tamper matrix, coverage reporting, bytes unchanged | Exit 0/2/3/4 correct; never writes |
| **P5.5.7** | Rotation, manifest, archives, retention documentation, operator runbook; monthly cadence | P5.5.6 | Automatic deletion/schedule | Four crash-stage tests, manifest continuity, immutability, retention entries | Rotation preserves chain; crash states detected; no silent mutation of history |
| **P5.5.8** | Security regression, fault injection, exclusion sweep, docs and acceptance | P5.5.1–P5.5.7 | P5.6 work | Full matrix + soak + review | All criteria met; owner acceptance |

Rotation is deliberately **P5.5.7**; P5.5.3 implements only the single active
journal.

---

## 14. Owner decisions (approved before P5.5.2)

These were the residual questions in the P5.5.1 review; the project owner has
resolved all seven. They are binding on P5.5.2 and every later sub-milestone.

1. **Login failures store no username.** `target_type="account"`,
   `target_id=null`. The normalized username is never retained in an audit
   record, including on rate-limited and invalid-credential events.
2. **Restricted mode is the startup policy.** A corrupt or unverifiable audit
   store puts the application in restricted mode: reads, `/health` and the
   existing authentication routes keep working, while audited mutations are
   blocked. Strict refuse-to-start remains an optional mode
   (`AUDIT_REQUIRE_INTEGRITY`), not the default and not part of P5.5.2.
3. **`authz.denied` may record the route template and HTTP method** (never a
   query string, path parameters, request body or header). This is the
   `endpoint` + `method` metadata pair.
4. **Retention is at least 365 days, archive-only initially**, with no
   automatic deletion and no deletion schedule.
5. **A missing declared historical segment always prevents new writes**, even
   when a manifest anchor exists. Anchor-only coverage is a reporting mode for
   the read-only verifier, never a licence to append.
6. **`AUDIT_UNAVAILABLE` and `AUDIT_RESULT_UNCERTAIN` are approved** with the
   fixed messages in §10.1.
7. **An unknown database outcome is recorded as `DATABASE_OUTCOME_UNKNOWN`**
   and its API response must explicitly communicate that the outcome is
   unknown; it must never imply definite failure.

Still deferred to later sub-milestones (not open design questions): the exact
archive-offsite wording and 365-day operator procedure (P5.5.7).

## 15. P5.5.2 implementation notes

Implemented in `src/audit_schema.py` (pure, no I/O) and covered by
`tests/test_audit_schema.py`. This section records the concrete choices made
where the design record states a rule but not its mechanical form.

### 15.1 Module contents

* Closed vocabularies: `AuditAction` (7), `AuditOutcome` (4),
  `AuditReasonCode` (20), `AuditSource` (3), `AuditTargetType` (5) — all
  `str`-backed enums; raw strings are refused by the event constructor and
  converted only by `AuditEvent.from_dict`.
* `AuditEvent`: frozen dataclass with all 19 fields, validating every rule in
  §2 at construction: supported `schema_version` (exactly `1`), positive
  global and segment-local sequences, `sequence >= segment_sequence`,
  `segment_id` matching `YYYY-MM`, canonical lowercase UUIDv4 `audit_id`,
  `correlation_id` and `request_id`, the fixed UTC microsecond timestamp, the
  actor and target nullable pairs, outcome/reason coherence, the metadata
  allowlist and the hash formats.
* `validate_metadata`, `canonical_json_text`, `canonical_json_bytes`,
  `format_timestamp`, `parse_timestamp`, `compute_record_hash`,
  `verify_record_hash`, `verify_chain` and `ChainVerification`.
* Constants: `AUDIT_SCHEMA_VERSION`, `GENESIS_PREVIOUS_HASH` (64 zeroes),
  `MAX_RECORD_BYTES` (8192), `MAX_METADATA_KEYS` (16), `MAX_METADATA_BYTES`
  (1024), `MAX_TARGET_ID_LENGTH` (256), `METADATA_ALLOWED_KEYS`.

### 15.2 Rules clarified for implementation

* **Metadata values are `str`, `int` or `bool` only.** Floats are refused
  outright — finite or not — so NaN/Infinity can never reach the serializer
  through metadata. Nesting, arrays and `None` are refused by the same rule.
* **Per-key metadata rules:** `endpoint` must match
  `/[A-Za-z0-9._~{}/-]*` (no scheme, query, fragment or whitespace, ≤ 256
  characters); `method` must be an uppercase HTTP method; `http_status` must
  be an `int` (never `bool`) in 100–599; `employee_active_after` must be a
  `bool`; `journal_event_id` a non-empty control-character-free string ≤ 256;
  `prior_role`/`new_role` an approved role name. Allowlisted string values are
  capped at 256 characters; the 1 KiB and 16-key bounds are enforced
  independently as defensive limits for future allowlist growth.
* **Outcome/reason coherence** is enforced: `OK` ⇔ `success`;
  `PERMISSION_DENIED` is a denial reason, and the three uncertainty codes
  (`AWAITING_RESULT`, `DATABASE_OUTCOME_UNKNOWN`, `PERSISTENCE_UNCONFIRMED`)
  are recorded with `outcome="uncertain"` by the service layer. The schema
  itself enforces only the `OK`/`success` equivalence.
* **Correlation rules:** `auth.login`, `auth.logout` and `authz.denied` never
  carry `correlation_id`; a mutation record without one must be the intent
  record (`reason_code=AWAITING_RESULT`).
* **`segment_id` is `YYYY-MM-NNN` and is validated for format only, not
  coupled to the record's timestamp month.** The prefix is the segment's
  allocation month; event time is an event fact. Demanding equality would make
  every append fail in the window between a calendar month rollover and the
  operator's rotation (and after crash recovery), which the design does not
  permit.
* **The constructor validates `record_hash` format but does not recompute it.**
  Digest consistency is a verification property (`verify_record_hash`,
  `verify_chain`) so a tampered record can still be read and reported instead
  of making verification fail to load its own evidence. `AuditEvent.create`
  always computes the digest.
* **The 8 KiB record bound and the 1 KiB/16-key metadata bounds are enforced at
  construction** on canonical bytes. Because the current allowlist can only
  reach roughly 2 KiB, the bounds are defensive: tests exercise them by
  lowering the constants/allowlist, and they guard future field growth.
* **Hash input** is the canonical body including `previous_hash` and excluding
  `record_hash`; `compute_record_hash` accepts a body that omits
  `record_hash` entirely, which is the form the writer will hash.

### 15.3 Chain verification contract

`verify_chain(records, *, expected_previous_hash=GENESIS_PREVIOUS_HASH,
 expected_next_sequence=1)` accepts `AuditEvent` values or parsed mappings and
checks, per record: schema validity, predecessor link, global sequence,
segment-local sequence (1 at each segment start, then +1), new and ascending
segment ids (no reopening), and the recomputed digest. It returns a
`ChainVerification` summary (`record_count`, first/last global sequence, last
`record_hash`, segment ids). An empty input is valid and returns the zero
summary — an empty journal is a legitimate state, not an error.

The two keyword parameters are the P5.5.6/P5.5.7 extension seam for anchor
verification; their defaults describe a full chain from genesis. Nothing is
written, repaired or mutated: mappings are parsed into new immutable values.

### 15.4 Typed errors

All inherit `AuditError` (a `ValueError`) and carry optional `position` /
`sequence` attributes only: `AuditSchemaError`, `AuditTimestampError`,
`AuditMetadataError`, `AuditHashFormatError`, `AuditCanonicalizationError`,
`AuditHashMismatchError`, `AuditChainError`,
`AuditPreviousHashMismatchError`, `AuditSequenceError`, `AuditSegmentError`.
Messages name the field, rule, position or sequence — never a record value,
metadata content, identifier or digest.

### 15.5 Verification

172 focused tests in `tests/test_audit_schema.py` cover schema, vocabulary,
canonicalization, hashing (including a fixed known-answer digest), chain
verification, same-month multi-segment chains, segment-identity rules and the
tamper matrix. Nothing in P5.5.2 writes a file, so
filesystem durability, concurrency and crash behaviour remain unimplemented
and untested (P5.5.3).

## 16. Segment identity — approved `YYYY-MM-NNN` correction (applied in P5.5.2)

This section records the P5.5.2 segment-identity review and the owner-approved
correction, which **is applied** in `src/audit_schema.py` and its tests. It had
to be resolved before P5.5.3 because the writer's startup verification and the
rotation crash-recovery rules depend on it.

### 16.1 Finding: the month-only schema could not represent two rotations in one month

Before the correction, `src/audit_schema.py` accepted only
`^\d{4}-(0[1-9]|1[0-2])$` for `segment_id` (§2), and `verify_chain` (a)
rejected any segment id that appeared earlier in the chain — "segment ids must
not repeat; a sealed segment cannot be reopened" — and (b) required each new
segment id to be lexicographically greater than its predecessor. Together these
made a second segment in the same calendar month both unrepresentable and
unverifiable:

1. An operator seals `2026-11` and creates the next active segment. The only
   schema-valid identifier for November is `2026-11`, so the new segment
   repeats the sealed id.
2. On the first append to the new segment, `verify_chain` sees a repeated id and
   raises `AuditSegmentError`. Under P5.5.3 that is not a warning: the writer
   verifies the whole available chain at startup and refuses to become `ready`,
   so **all audited mutations return `503 AUDIT_UNAVAILABLE`** (restricted
   mode) until an operator intervenes. A legitimate, documented operator action
   (incident response, pre-maintenance seal, size pressure) thus causes an
   audit-availability outage and a false integrity alarm.
3. The rotation crash table (§9.4) has the same problem at two stages: "after
   rename, before manifest entry" (register an existing archive) and "after
   manifest entry, before new active file" (create a replacement segment). Both
   create or register a segment whose id must be distinct from every sealed
   segment — which a month-only id cannot express when the crash and the
   rotation fall in the same month.
4. Re-running a failed rotation is meant to be safe; with month-only ids a
   retry after a partial seal cannot allocate a fresh, distinct id.

This is a genuine correctness defect in the schema's segment model, not a style
issue: it makes an approved operational capability (operator-controlled
rotation, "monthly target cadence") impossible to perform more than once per
month and turns the second attempt into a fail-closed outage.

### 16.2 Why the month-only rule seemed sufficient, and what it actually proves

`YYYY-MM` was chosen so a human can see which month a segment covers, and the
lexical comparison was a cheap sanity check. But "monthly" is a **cadence
target**, not a uniqueness guarantee, and rotation timing is operator-controlled
rather than clock-driven. Lexical month ordering also proves nothing about
actual rotation order: it is implied today only because the identifier *is* the
month. The chain already proves order cryptographically and numerically — the
predecessor `record_hash` link, the global `sequence` continuation and the
segment-local restart at 1 — so the label check adds no integrity, only the
false-negative above.

There is no data to migrate: no journal, segment or manifest exists yet
(P5.5.3 is unimplemented). Correcting the format now is free; after the writer
ships it would require coordinated archive/manifest handling.

### 16.3 Approved identifier: `YYYY-MM-NNN`

`segment_id = <allocation month YYYY-MM>-<3-digit ordinal NNN>` (e.g.
`2026-11-001`, `2026-11-002`, `2026-12-001`):

* `YYYY-MM` is the UTC month in which the segment was **allocated** (its first
  active-file creation), not a claim about every record's timestamp (§16.5).
* `NNN` is the allocation ordinal within that month, `001`–`999`, starting at
  `001`. Any number of rotations in a month is representable; the format has
  room for 999 and the bound is validated (no `000`, no overflow).
* **Uniqueness is global** across all segments, current and archived, and is
  recorded in the manifest (`segment_sealed` is unique per `segment_id`, §9.2).
* Allocation is deterministic and operator-independent of the clock date:
  * normal rotation: next ordinal for the allocation month, where the month is
    the current UTC month at rotation time;
  * empty active file after a crash: the id is derived from the sealed
    predecessor — same allocation month as the sealed segment, ordinal + 1 — so
    repeated restarts compute the same id and two rotations in one month stay
    distinct even if the first append happens later;
  * registering an archive produced by an interrupted rotation: the id is read
    from the archive's own first record (all records in a segment share it), not
    re-allocated, and the seal entry is appended with that id;
  * after `999`, the rotation tool refuses and requires an operator decision —
    it never silently reuses or renames.
* Rationale for keeping a month prefix while making it an allocation label: it
  preserves operator readability and rough sortability, it is deterministic
  (unlike a random suffix, which makes a retried rotation able to produce two
  ids for one intended seal), and it needs no new dependency (unlike a ULID).
  Ordering is never taken from the string.

Alternatives considered:

| Option | Failure case |
|---|---|
| Keep `YYYY-MM` | The defect above: second rotation per month impossible; fail-closed outage |
| Opaque UUIDv4 | Unique, but no operator-readable link to when the segment was opened and no deterministic retry/recovery; ordering rests entirely on links/manifest |
| `YYYY-MM-<random 8 hex>` | Unique without bookkeeping, but a retried rotation can allocate a second id for the same intended seal, and ids do not sort or reveal missing allocations |
| `YYYY-MM-NNN` (recommended) | Requires the rotation tool to read the manifest (already required to append seals) and caps a month at 999 segments (hit only by pathological rotation; explicit refusal) |

This design is approved and applied. An opaque identifier was considered and
rejected for the operator-readability and deterministic-retry reasons above;
if one were ever adopted, §16.4–§16.9 would still apply, with only the format
rule and allocation derivation changing.

### 16.4 Verification and ordering after the change

* **Remove** the lexicographic `segment_id` ordering rule from `verify_chain`.
* **Keep** the "an id never repeats" rule: an id identifies exactly one
  segment, so a re-appearance means a spliced or duplicated chain. This is
  independent of the label format and remains a real integrity check.
* **Keep** predecessor-link continuity, global sequence `+1`, segment-local
  restart at 1, and the requirement that the first record of a segment has
  `segment_sequence == 1`.
* **Order proof** is therefore: hash linkage (cryptographic), global sequence
  (numeric), manifest seal order and anchors (P5.5.7). A verifier note may report
  the allocation months for human context, but never as a pass/fail condition.
* The future manifest cross-check (P5.5.6/P5.5.7) becomes the authority for seal
  order and for continuity across archived/missing segments; the event chain
  must not depend on how labels sort.

### 16.5 Timestamp-to-segment policy

**Event timestamps must not be required to match the segment's calendar
period.** The schema validates timestamp form and precision only; the writer
and verifier must not reject a record because `timestamp[:7] != segment_id[:7]`.

Reasons: rotation is operator-controlled and may lag or lead the calendar; the
active segment legitimately receives records after a UTC month rollover until
the operator rotates; crash recovery may resume an older segment after the
month changes; and a clock seam or restore can move event time independently of
segment allocation. Month equality would convert each of these benign states
into a fail-closed mutation outage or a false integrity failure.

What is required instead:

* `timestamp` is the authoritative event time (fixed UTC microsecond form);
  retention and reporting use it, never `segment_id`.
* The writer requires non-decreasing timestamps within the active segment
  (clock discipline; backwards time is an operational anomaly, not silently
  reordered history).
* The manifest seal entry may record first/last timestamps for operators; that
  is descriptive metadata, not an integrity gate.
* A verifier may print the allocation month and the observed timestamp range as
  information, and must not fail a run on a mismatch.

### 16.6 Implementation impact (applied)

The change is confined to P5.5.2's pure layer:

* `src/audit_schema.py`: `_SEGMENT_PATTERN` is
  `^([0-9]{4})-(0[1-9]|1[0-2])-([0-9]{3})$` with `001`–`999` (`000` rejected
  explicitly); the error message names the format; the
  `record.segment_id <= previous.segment_id` branch was removed from
  `verify_chain` (the repeated-id branch remains). No other field, hash,
  canonicalization or metadata rule changed; `segment_id` is hashed as before.
* `docs/AUDIT_JOURNAL_DESIGN.md` §2/§4: replace the `YYYY-MM` rule and the
  lexical-order sentence; §9.3/§9.4: state the allocation rule for new and
  recovered segments.
* No writer, manifest, CLI or route change is part of this: P5.5.3 consumes the
  corrected format later.

### 16.7 P5.5.4 enforcement matrix (action / outcome / reason)

The schema deliberately enforces only structural coherence (`OK` ⇔ `success`,
standalone events never correlate, a mutation record without correlation is the
intent). The per-action matrix below is **P5.5.4's responsibility**; it must
refuse any combination not listed, before writing, so an impossible event can
never be constructed by a route.

| Action | Intent | Allowed result (outcome / reason) | Explicitly forbidden |
|---|---|---|---|
| `employee.create` | `uncertain` / `AWAITING_RESULT` | `success`/`OK`; `error`/`EMPLOYEE_EXISTS`, `EMPLOYEE_INVALID`, `VALIDATION_FAILED`, `DATABASE_UNAVAILABLE`, `INTERNAL_ERROR`; `uncertain`/`DATABASE_OUTCOME_UNKNOWN` | `denied`, `PERMISSION_DENIED`, `EMPLOYEE_NOT_FOUND`, `EMPLOYEE_INACTIVE`, checkout-only and auth-only codes |
| `employee.update` | same | `success`/`OK`; `error`/`EMPLOYEE_NOT_FOUND`, `EMPLOYEE_INVALID`, `VALIDATION_FAILED`, `DATABASE_UNAVAILABLE`, `INTERNAL_ERROR`; `uncertain`/`DATABASE_OUTCOME_UNKNOWN` | `denied`, `EMPLOYEE_EXISTS`, `EMPLOYEE_INACTIVE`, checkout-only and auth-only codes |
| `employee.deactivate` | same | `success`/`OK`; `error`/`EMPLOYEE_NOT_FOUND`, `EMPLOYEE_INACTIVE`, `VALIDATION_FAILED`, `DATABASE_UNAVAILABLE`, `INTERNAL_ERROR`; `uncertain`/`DATABASE_OUTCOME_UNKNOWN` | `denied`, `EMPLOYEE_EXISTS`, `EMPLOYEE_INVALID`, checkout-only and auth-only codes |
| `attendance.checkout` | same | `success`/`OK`; `error`/`EMPLOYEE_NOT_FOUND`, `EMPLOYEE_INACTIVE`, `NO_OPEN_SESSION`, `CHECKOUT_COOLDOWN`, `VALIDATION_FAILED`, `DATABASE_UNAVAILABLE`, `INTERNAL_ERROR`; `uncertain`/`DATABASE_OUTCOME_UNKNOWN`, `PERSISTENCE_UNCONFIRMED` | `denied`; `PERSISTENCE_UNCONFIRMED` with any other outcome; `EMPLOYEE_EXISTS`, `EMPLOYEE_INVALID`, auth-only codes |
| `auth.login` | none | `success`/`OK`; `denied`/`INVALID_CREDENTIALS`, `ACCOUNT_INACTIVE`, `RATE_LIMITED`; `error`/`DATABASE_UNAVAILABLE`, `INTERNAL_ERROR`; `uncertain`/`DATABASE_OUTCOME_UNKNOWN` (session-persistence ambiguity) | `AWAITING_RESULT`, any `correlation_id`, `PERMISSION_DENIED`, employee/checkout codes, `SESSION_*` |
| `auth.logout` | none | `success`/`OK` (idempotent 204); `error`/`SESSION_INVALID`, `DATABASE_UNAVAILABLE`, `INTERNAL_ERROR`; `uncertain`/`DATABASE_OUTCOME_UNKNOWN` | `AWAITING_RESULT`, any `correlation_id`, `denied`/`PERMISSION_DENIED`, employee/checkout/login codes |
| `authz.denied` | none | `denied`/`PERMISSION_DENIED` only | every other reason; every other outcome; any `correlation_id`; a null actor |

Cross-cutting rules P5.5.4 must also enforce:

* `outcome=denied` only for `auth.login` and `authz.denied`; mutation refusals
  are `error`, never `denied` (a mutation is not attempted when RBAC denies it —
  that is an `authz.denied` event).
* `AWAITING_RESULT` only as the intent of the four mutation actions, always with
  `outcome=uncertain` and no `correlation_id`.
* `DATABASE_OUTCOME_UNKNOWN` always `uncertain` (decision 7); never `success`,
  `denied` or `error`.
* `PERSISTENCE_UNCONFIRMED` only for `attendance.checkout` and always
  `uncertain`.
* `OK` only with `success` (also schema-enforced); `PERMISSION_DENIED` only for
  `authz.denied`; `EMPLOYEE_*` only for the three employee actions and checkout;
  `NO_OPEN_SESSION`/`CHECKOUT_COOLDOWN` only checkout;
  `INVALID_CREDENTIALS`/`ACCOUNT_INACTIVE`/`RATE_LIMITED` only login;
  `SESSION_ABSENT`/`SESSION_INVALID` only logout.
* `JOURNAL_CORRUPT` is **not emitted** by P5.5.4/P5.5.5: a corrupt journal is a
  fail-closed state reported through logs and restricted mode, not an event
  written into the journal it describes. It stays reserved for a possible
  `source=system` event in a future, separately approved design.
* `actor_user_id`/`actor_role` are set for every authenticated event;
  `authz.denied` always has an actor; null actors occur only for pre-session
  `auth.login`/`auth.logout`.
* A logout request with no session cookie produces **no** audit event (204
  unchanged, nothing happened); a cookie that resolves to no valid session is
  `auth.logout`/`error`/`SESSION_INVALID` (best-effort).
* Target types: `employee` + id for the three employee actions and checkout;
  checkout may add `journal_event_id` metadata when known; `account` + user id
  on successful login, `account` + `null` on every login failure (decision 1);
  `account` + user id for an authenticated logout, `null`/`null` for a
  pre-session logout; `null`/`null` plus `endpoint`/`method`/`http_status=403`
  metadata for `authz.denied` (decision 3).
* Result records always carry `correlation_id` = the intent's `audit_id`;
  a result is never written without its intent.

### 16.8 Tests after the correction

The suite is now 172 tests (159 before): fixtures use `YYYY-MM-NNN`, and the
old ascending-lexical test was replaced. Updated: `make_event`/`build_chain`
fixtures (`2026-11` → `2026-11-001`), `test_invalid_segment_id_is_rejected`
(10 invalid forms including month-only, `0000`, `1000`, `00a`),
`test_empty_chain_is_valid_and_explicit`, `test_valid_single_record_chain` and
`test_valid_chain_spanning_two_segments`. The known-answer digest was
recomputed for the new fixture segment id.

Tests added:

1. `test_two_segments_allocated_in_the_same_month` — `2026-11-001` then
   `2026-11-002` verifies; boundary links to the predecessor hash and
   `segment_sequence` restarts at 1.
2. `test_same_month_segment_boundary_links_to_predecessor_hash`.
3. `test_repeated_segment_id_is_rejected_even_when_hashes_and_sequences_are_valid`.
4. `test_zero_ordinal_is_rejected`, `test_four_digit_ordinal_is_rejected`,
   `test_maximum_ordinal_is_accepted` (`999`, `2026-01-001`).
5. `test_month_only_segment_id_is_rejected`.
6. `test_non_lexically_ordered_segment_labels_are_accepted` (order from links
   and sequences, not the string).
7. `test_timestamp_month_may_differ_from_segment_allocation_month`.
8. `test_segment_local_sequence_restarts_at_one_at_a_new_segment`.

Manifest/rotation-side tests remain P5.5.7, not this layer.
