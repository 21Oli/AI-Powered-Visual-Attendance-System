# P5.9 — Phase 5 final security review and acceptance readiness

**Status:** review complete, **one source defect found and fixed**; **not accepted**.
Phase 5 acceptance is the owner's decision and is deliberately withheld here.

**Review date:** 2026-11-20.
**Branch / HEAD at review:** `main`, `490ca2a8f92ef29d26a78aa5f81da5f2a72703f1`.
**Working tree at review start:** clean (nothing staged, nothing untracked).
**Working tree after this review:** the seven P5.9 paths in §17 (five modified tracked
files, two untracked files), nothing staged, nothing committed. Same branch and HEAD.

**Executive summary:** the integrated source review is complete across authentication,
RBAC, user administration, audit integrity/rotation, database safety and frontend
transport. Every automated suite, lint, typecheck, build, compilation and whitespace
check run for this milestone passed. One medium-severity audit-traceability defect (F1)
was found and fixed with four regression tests; two low-severity items (F2, F3) are
disclosed rather than changed because fixing them would alter the approved audit event
matrix. Real-PostgreSQL concurrency verification remains **BLOCKED** on owner-side
database setup, and no owner-acceptance record exists in the repository for
P5.6/P5.7/P5.8/P5.5.8. Phase 5 is **not** accepted by this document.

**Recommendation:** **READY FOR OWNER ACCEPTANCE — WITH DISCLOSED LIMITATIONS**, conditional
on the owner accepting the two remaining product limitations (no user-management UI, no
audit dashboard) and the still-**BLOCKED** real-PostgreSQL concurrency verification.

---

## 1. Phase 5 scope

Phase 5 adds operator identity, sessions and role-based access to the existing attendance
system without changing employee or attendance semantics and without introducing biometric
storage:

| Milestone | Scope |
|---|---|
| P5.1 | Inspection, gap analysis, approved decisions (review only) |
| P5.2 | Identity schema (`V002__security.sql`), user/session models and repositories |
| P5.3 | Argon2id hashing, opaque session tokens, session lifecycle |
| P5.4 | Authentication API: login/logout/`/me`, BFF transport, throttling, backend RBAC (P5.4.1–P5.4.10) |
| P5.5 | Append-only audit journal, hash chain, verifier, rotation, manifest, retention, evidence pack |
| P5.6 | User administration API, first-Admin provisioning CLI, admin-invariant locking, Tier-2 database guard |
| P5.7 | Frontend authentication: sign-in, session restoration, protected routes, sign-out |
| P5.8 | Role-aware UI, centralized presentation permissions, authorization error handling |
| P5.9 | This integrated review and acceptance preparation |

Out of scope throughout: Phase 6/7 work, migrations executed against any database, real
employee/attendance/audit/biometric data, and any change to the computer-vision or
attendance-recording pipeline.

## 2. Final architecture overview

* **Identity and sessions (P5.2/P5.3/P5.4).** `app_user` and `user_session` in PostgreSQL.
  Passwords are Argon2id (`src/password.py`); session tokens are 256-bit `secrets` values
  whose SHA-256 digest is the `user_session` primary key (`src/session_token.py`), with a
  sliding 8-hour idle bound and an immutable 12-hour absolute bound
  (`src/session_service.py`). The browser only ever sees the `va_session` cookie.
* **Transport (P5.4.8).** The Next.js BFF (`frontend/src/lib/server/proxy.ts`,
  `handler.ts`) exposes exactly two entry points — `/api/backend/...` and
  `/api/v1/auth/...` — behind one strict method/path allowlist. `BACKEND_BASE_URL` is
  server-only; `Origin` is forwarded only for login/logout; only `va_session` is forwarded
  upstream; `Set-Cookie` is played back per header.
* **Authorization (P5.4.10).** `src/authz.py` is the single authority: eight permissions,
  one immutable four-role table, one pure decision function, one FastAPI dependency factory
  that wraps the existing session dependency. Identity is never read from a body, query or
  header.
* **Audit (P5.5).** A separate append-only JSONL journal (`data/audit/audit.jsonl`) with
  canonical JSON, SHA-256 chaining, a global monotonic sequence, segment identity, a
  separately chained manifest, operator-controlled rotation and a read-only offline
  verifier. Mandatory mutations use a durable intent/result protocol and fail closed.
* **User administration (P5.6).** `src/user_service.py` + `src/api_user.py` (four
  Admin-only routes) + `src/user_provision.py` (CLI only). The last-active-Admin invariant
  is decided inside one `READ COMMITTED` advisory-lock transaction in
  `src/user_repository.py`.
* **Frontend (P5.7/P5.8).** `AuthProvider` restores the session from `/api/v1/auth/me`,
  `RequireAuth` guards the dashboard, `RequirePermission` plus `useAuthorization` gate
  pages, `navigation.ts` filters the chrome, and privileged controls are hidden — never
  merely disabled — before the session resolves. The frontend mirror
  (`frontend/src/lib/authz/permissions.ts`) is presentation only.

## 3. Prerequisite / acceptance matrix

Verified from repository evidence and commit history, not from milestone prose.

| Milestone | Implemented | Committed | Independently reviewed | Owner accepted | Verification blocked |
|---|---|---|---|---|---|
| P5.5 | Yes — `src/audit_*.py`, `tools/audit_verify.py`, `tools/audit_rotate.py`, 15 test files | Yes — `c336f33`, `f88551d`, `b94ade4`, `4dcb154`, `87c73e5`, `c2871f3`, `012ae31`, `acdade1`, `fd1d063`… (see §4) | **No** — no review record in the repository | **Partially**: P5.5.6 accepted (`87c73e5`), P5.5.7.1/P5.5.7.2 accepted (`c2871f3`); **P5.5 as a whole not accepted** | No — offline evidence complete |
| P5.6 | Yes — `src/user_service.py`, `src/api_user.py`, `src/user_provision.py`, `src/user_repository.py` | Yes — `fd1d063`, `d4fa22f` | **No** | **No** — no acceptance record | **Yes** — real-PostgreSQL concurrency suite (§9) |
| P5.7 | Yes — `frontend/src/lib/auth/*`, `components/auth/*`, `app/login` | Yes — `246a2f6` | **No** repository record | **Not recorded** — the owner accepted it in conversation, but `docs/P5_7_FRONTEND_AUTHENTICATION.md` still says "awaiting review and owner acceptance" | No |
| P5.8 | Yes — permission mirror, guards, nav/action gating, 401/403/503 handling | Yes — `490ca2a` | **No** repository record | **Not recorded** — `docs/P5_8_ROLE_AWARE_UI_SECURITY.md` still says "awaiting independent review and owner acceptance" | No |

**What is missing, stated plainly:**

1. No owner-acceptance record for P5.6, P5.7 or P5.8 exists anywhere in the repository.
   Acceptance that happened only in conversation is **not** repository evidence and is not
   treated as such here.
2. No independent-review record exists for P5.5.2–P5.5.5, P5.5.7, P5.5.8, P5.6, P5.7 or
   P5.8. "Implemented, awaiting review" in the milestone documents is aspirational until a
   reviewer writes something down.
3. P5.7's acceptance status is *unrecorded*, not *absent*: the discrepancy between the
   conversation and the document is flagged for the owner to settle in one line either way.

## 4. Milestone evidence status

### 4.1 P5.5 — audit subsystem

* Implemented across `src/audit_schema.py`, `audit_writer.py`, `audit_service.py`,
  `audit_manifest.py`, `audit_manifest_store.py`, `audit_archive.py`, `audit_rotation.py`,
  `audit_layout.py`, `audit_locking.py`, `api_audit.py`, `api_login.py`, `api_logout.py`,
  `api.py`, `tools/audit_verify.py`, `tools/audit_rotate.py`.
* Evidence pack: `docs/P5_5_AUDIT_ACCEPTANCE_EVIDENCE.md` (recorded 15-file audit suite
  **780 passed, 1 skipped**; full backend suite **3871 passed, 33 skipped** at that time).
* **Naming reconciliation (historical, not rewritten).** The pack's §D lists both
  `P5.5.7.1` and `P5.5.7.2` against the same commit `c2871f3` and the same scope
  ("manifest schema / integrity"). `docs/PHASE5_IMPLEMENTATION_PLAN.md` §9 tracks the same
  delivered unit only as **P5.5.7.2**. The two identifiers therefore name one delivered
  milestone; `P5.5.7.1` is a duplicate label, not a second implementation. Both doc
  statements are preserved as historical records; this note is the reconciliation, and the
  canonical identifier from here on is **P5.5.7.2**. No acceptance is invented for a
  "P5.5.7.1" deliverable that the plan never had.
* P5.5.8 (security regression, exclusion sweep, failure/recovery, soak, docs, evidence) is
  implemented; its own acceptance is **PENDING OWNER ACCEPTANCE**.

### 4.2 P5.6 — user administration

* Implemented: service, repository invariants, four Admin-only endpoints, CLI provisioner,
  offline suites. See `docs/P5_6_ACCEPTANCE_EVIDENCE.md`.
* **Real-PostgreSQL concurrency verification remains BLOCKED** (§9). No document in the
  repository claims otherwise; this review did not change that.

### 4.3 P5.7 — frontend authentication

* Implemented: `LoginForm`, `AuthProvider`, `RequireAuth`, `UserMenu`, `/login`,
  safe internal redirect paths (`src/lib/auth/paths.ts`), neutral first-visit messaging,
  and the documented `Path=/api/v1` cookie limitation.
* Verified again in this review: no token is written to `localStorage`/`sessionStorage`
  (the only `localStorage` use in the frontend is the theme preference), and the frontend
  guard never substitutes for backend authentication.

### 4.4 P5.8 — role-aware frontend

* Implemented: mirrored permission matrix, `useAuthorization`, `RequirePermission`,
  `AccessDenied`, role-filtered `navigation.ts`, hidden privileged actions with
  handler-level re-checks, 401 broadcast (`session-events.ts`), 403 session preservation,
  503/network non-logout.
* Verified in this review: the frontend matrix matches `src/authz.py` exactly; unknown
  roles/permissions fail closed; the P5.8 commit touched **no** proxy or allowlist file.

## 5. Authentication review

| Check | Result | Evidence |
|---|---|---|
| Backend authentication remains authoritative | PASS | `src/api_auth.py` cookie-only dependency; `src/authz.py` wraps it |
| Session identifier delivered only via HttpOnly cookie | PASS | `src/api_cookies.py`, `SESSION_COOKIE` (`va_session`, `Path=/api/v1`, `SameSite=Lax`, `Secure` outside development) |
| No token in `localStorage`/`sessionStorage` | PASS | grep over `frontend/src`; tests assert empty storage keys |
| No second frontend auth system | PASS | one `AuthProvider`, one BFF entry point |
| Login/session restoration/logout use existing contracts | PASS | `api_login.py`, `api_me.py`, `api_logout.py`, `frontend/src/lib/api/auth.ts` |
| Expiry and auth errors handled safely | PASS | neutral first-visit copy; "expired" only when this tab held a session; 401 → clear state and safe redirect |
| No secrets or session identifiers in logs/client config | PASS | sanitized messages; `AuthProvider`/`client.ts` handle status only |
| `Path=/api/v1` limitation not overclaimed | PASS | documented in P5.7 §6; server components cannot read the cookie on unrelated paths |

## 6. Authorization matrix and enforcement

Backend policy (`src/authz.py`) and the frontend mirror
(`frontend/src/lib/authz/permissions.ts`) agree exactly:

| Permission | Admin | HR Manager | Operator | Auditor |
|---|---:|---:|---:|---:|
| employees:read | Yes | Yes | Yes | Yes |
| employees:create | Yes | Yes | Yes | No |
| employees:update | Yes | Yes | Yes | No |
| employees:deactivate | Yes | No | No | No |
| attendance:read | Yes | Yes | Yes | Yes |
| attendance:checkout | Yes | Yes | Yes | No |
| audit:read | Yes | No | No | Yes |
| users:manage | Yes | No | No | No |

| Check | Result | Evidence |
|---|---|---|
| Backend is the real boundary | PASS | every route depends on `permission_dependency` over the session principal |
| Frontend permissions are presentation only | PASS | module docstring + design; no client value reaches a decision |
| Every protected route enforces its declared permission | PASS | seven business routes + four user routes; `APPROVED_ROUTES` tests |
| Unknown roles/permissions fail closed | PASS | `permissions_for` returns `frozenset()` for anything not a `Role`; frontend denies unknown strings |
| Self-role-change and self-deactivation prevented | PASS | `UserService._assert_not_self_*`, tested |
| Last active Admin cannot be demoted/deactivated | PASS | repository advisory-lock transaction, tested offline |
| Client-supplied role cannot override authorization | PASS | identity only from the validated session principal |
| No mass assignment | PASS | `extra: "forbid"` request models; update models limited to declared fields |

**Observation (not a defect):** `Permission.AUDIT_READ` guards no endpoint, because no
audit read API exists. It is a policy definition, and no UI claims an audit capability.

## 7. Proxy and transport review

| Check | Result | Evidence |
|---|---|---|
| Exact method/path allowlist | PASS | 11 rules (health, login, `/me`, logout, 8 business routes); `tests/proxy.test.ts` (65 tests) |
| No open proxy | PASS | no wildcard, no client-supplied upstream URL |
| `BACKEND_BASE_URL` server-only | PASS | read in `src/lib/server/*`; no `NEXT_PUBLIC_` exposure of it |
| Cookie forwarding | PASS | only `va_session` forwarded upstream to `/me`/logout |
| `Origin` forwarding for login/logout | PASS | `forwardsOrigin` on exactly those two rules; backend validates against `TRUSTED_ORIGINS` |
| No unintended CORS | PASS | no CORS middleware in the BFF or backend |
| Body limits and timeouts | PASS | proxy body rule + request timeout enforced in `handler.ts` |
| No sensitive response detail leakage | PASS | sanitized error envelopes; upstream bodies limited to allowlisted routes |
| 401/403/503/uncertain outcomes handled | PASS | typed client errors, no blind mutation retry |

**Unchanged in this milestone:** no allowlist entry was added, removed or widened. The
user-management routes remain unreachable from the browser (§12).

## 8. Audit integrity and rotation review

| Area | Check | Result |
|---|---|---|
| Journal | JSONL is the sole attendance journal; audit is a separate append-only JSONL file | PASS |
| Canonicalisation | Canonical JSON + SHA-256 chain; genisis and segment links | PASS |
| Sequence | Global monotonic `sequence`; per-segment `segment_sequence`; no reset on rotation | PASS |
| Comparison | Hash comparison uses `hmac.compare_digest` | PASS |
| Tamper detection | Modified field, deleted record, reordered pair, forged digest all fail | PASS |
| Writer trust | Full chain verified before first append; no cached tail hash | PASS |
| Poisoning | Ambiguous write/flush/fsync poisons the writer; no retry/rollback/truncation | PASS |
| Coverage | Employee create/update/deactivate, checkout, user create/role change/deactivate, first-Admin provision are mandatory intent/result | PASS (see finding F2) |
| Privacy | Best-effort auth/authz events; no password, hash, token, cookie, biometric or personal data | PASS |
| Rotation | Manifest chain and continuity; archive path restrictions; symlink refusal; torn manifest recovery | PASS |
| Retention | Explicit decisions; **no** automatic deletion | PASS |
| Verification | Offline, read-only, exit codes `0/1/2/3/4/5` | PASS |

**Documented limitations preserved (not upgraded by this review):** read-only filesystem
permissions are not true immutability; verification is offline; no distributed or
network-filesystem safety guarantee; no snapshot-consistency claim; anchor-only coverage is
reported honestly rather than as full-chain verification.

## 9. Database verification status

**BLOCKED — unchanged from P5.6, not assumed to have passed.**

`tests/test_tier2_user_concurrency.py` reports **7 skipped**, each with the same reason:

> Tier-2 PostgreSQL verification is opt-in: set `ATTENDANCE_TIER2_OPT_IN=1` with a guarded
> `ATTENDANCE_TEST_DATABASE_URL`.

Exact missing prerequisites:

1. The dedicated test database still lacks the V002 schema (`app_user`, `user_session`), so
   the migration would have to be applied there — an owner-authorized action.
2. The `attendance_tier2_cleanup` role does not exist; creating it and issuing the six
   least-privilege grants are owner actions.
3. The cleanup DSN must be supplied through `ATTENDANCE_TIER2_CLEANUP_DATABASE_URL`; this
   review was not authorized to read any credential source.

Not executed in this milestone: no migration, no SQL script, no role creation, no `GRANT`/
`REVOKE`, no connection to any database. The offline suites prove the *verification logic*
only (guard identity, cluster-identity handling, owner-script static review, effective
privilege checks); they are **not** evidence that PostgreSQL concurrency scenarios pass.

## 10. Integrated test results (this review)

Re-measured in the final revalidation pass (all counts below are from these runs, not
carried over from an earlier report):

| Suite | Command | Result |
|---|---|---|
| Focused (changed area) | `.venv/Scripts/python.exe -m pytest tests/test_user_audit_request_id.py tests/test_user_service.py tests/test_api_user.py -q` | **70 passed**, exit 0 |
| 15-file audit suite | the §C.1 command from `docs/P5_5_AUDIT_ACCEPTANCE_EVIDENCE.md` | **783 passed, 1 skipped**, exit 0 (142.72 s; 784 collected) |
| Tier-2 PostgreSQL file | `.venv/Scripts/python.exe -m pytest tests/test_tier2_user_concurrency.py -q -rs` | **7 skipped**, exit 0 — every skip cites the opt-in/guarded-DSN reason (§9) |
| Full backend | `.venv/Scripts/python.exe -m pytest -q` | **4006 passed, 40 skipped**, exit 0 (756.57 s) |
| Full frontend | `cd frontend && npx vitest run` | **614 passed, 42 files**, exit 0 |
| Python compilation | `.venv/Scripts/python.exe -m compileall -q src tools tests` | exit 0 |
| ESLint | `cd frontend && npm run lint` | exit 0, no findings |
| TypeScript | `cd frontend && npm run typecheck` | exit 0 |
| Production build | `cd frontend && npm run build` | exit 0; routes unchanged (`/`, `/_not-found`, `/api/backend/[...path]`, `/api/v1/auth/[action]`, `/attendance`, `/employees`, `/login`, `/overview`, `/system-status`) |
| Whitespace | `git diff --check` | exit 0 |

The **40 skips** are the pre-existing opt-in/offline skips (Tier-2 PostgreSQL and related
guarded scenarios); the seven Tier-2 skips were confirmed individually with `-rs` and each
cites the opt-in/guarded-DSN reason. No test expectation was weakened, removed or skipped
to obtain these results. The backend total moved 4002 → 4006 because this review added four
regression tests.

The 15-file audit total is **783 passed, 1 skipped** today, against the **780 passed, 1
skipped** recorded in the P5.5 evidence pack. Two of the difference is the exclusion file
now collecting 52 tests instead of 50 (§16, row 1, and that pack's §D.2); the remaining one
test is not attributable from the pack's per-group breakdown and is therefore reported as
measured, not reconciled by arithmetic.

## 11. Security review findings

### F1 (fixed) — audit records for user administration could not be correlated

* **Location:** `src/user_service.py` (all four mutations) and `src/api_user.py`.
* **Defect:** each user-management mutation built its `AuditContext` with
  `request_id=str(uuid.uuid4())` — a fresh random value — instead of the request's
  server-generated ID. Every other audited path (employee create/update/deactivate,
  checkout, login, logout) uses `request_id_for(request)`; `tests/test_api_audit.py` even
  pins `intent.request_id == echoed` for the employee path.
* **Impact:** audit records for `user.create`, `user.role_change`, `user.deactivate` and
  `first_admin.provision` could not be matched to the HTTP response (`X-Request-ID`), its
  structured logs or the request that produced them — a traceability failure on exactly the
  highest-privilege mutations, contradicting the approved contract ("Request ID correlation
  using the existing server-generated request ID", plan §9.2 and P5.5 acceptance
  criterion 4). It is not an access-control defect: authorization and the mutations
  themselves were correct, and no client value could influence the record.
* **Fix (smallest safe correction):** `UserService` takes an optional `request_id` and
  resolves it through `_audit_request_id` — explicit value first, then the current request's
  ID from `src.api_request_context`, and `None` when there is no request (CLI), because
  fabricating a UUID is worse than recording none. `src/api_user.py` passes
  `request_id_for(request)` on create, role change and deactivation.
* **Regression tests:** `tests/test_user_audit_request_id.py` (4 tests) asserts the route's
  audit context equals the response's `X-Request-ID`, that a client-supplied
  `X-Request-ID` cannot displace it, that all four mutations forward the given ID, and that
  no ID is fabricated outside a request.
* **Documentation:** `docs/P5_6_ACCEPTANCE_EVIDENCE.md` §12 records the correction.

### F2 (disclosed, not changed) — user actions reuse employee reason codes

`AuditReasonCode` has no user-specific members, and the approved per-action result matrix
(`src/audit_service.py::_MUTATION_RESULTS`) deliberately maps `USER_CREATE` /
`FIRST_ADMIN_PROVISION` duplicate-target failures to `EMPLOYEE_EXISTS` and
`USER_ROLE_CHANGE` / `USER_DEACTIVATE` missing-target failures to `EMPLOYEE_NOT_FOUND`.
This is an approved-design naming compromise, not an implementation deviation, so it was
**not** changed during final acceptance. It does make an audit reader's query for
"user not found" ambiguous with employee events. Recommendation: a separately authorized
audit-schema milestone adding `USER_EXISTS` / `USER_NOT_FOUND` (closed-vocabulary change +
manifest/verifier compatibility review).

### F3 (disclosed, not changed) — already-inactive deactivation returns success unaudited

`UserService.deactivate_user` returns the current inactive state and still calls
`revoke_all_for_user`, but writes no audit record in that branch (no user-state change
occurs). Session revocation is therefore not represented in the audit journal for that
retry path. Low severity — the account is already inactive and cannot authenticate — and
changing it would alter the documented audit event matrix, so it is recorded as a
limitation rather than fixed here.

### F4 (verified, no defect) — CLI provisioning actor

Provisioning records `AuditActor(user_id="cli", role=Role.ADMIN)`. The schema requires a
non-blank actor and a `Role`, and `source=cli` distinguishes the operator action. This is
accurate (the operator is acting with Admin authority) and contains no credential. It is
note-worthy for a future "system actor" refinement, not a defect.

### Assertions that held (no finding)

* No route lost its permission guard; `APPROVED_ROUTES` still covers all 15 routes.
* Unknown roles/permissions still fail closed on both sides.
* No secret, hash, token or biometric value is recorded or logged on any reviewed path.
* The proxy allowlist is byte-for-byte unchanged by P5.7/P5.8/P5.9.
* P5.8's test wrap (`withRole(...)`) is additive: assertions in the touched suites were
  re-read and none was removed or loosened.

## 12. Known limitations and remaining product limitations

**Product limitations (deliberate, not security defects):**

1. **No user-management frontend.** `/api/v1/users` is not in the proxy allowlist, and P5.8
   froze the allowlist. Building the screen requires a separately authorized proxy change
   plus the P5.6 database verification. The backend API exists and is Admin-only.
2. **No audit dashboard.** There is no audit read endpoint; `audit:read` guards nothing, and
   the verifier CLI is the only consumer. No UI claims otherwise.
3. **`/api/v1/users` unreachable from the browser today.** Admin user administration is
   API/CLI-level only.

**Verification limitations:**

4. Real-PostgreSQL concurrency verification is **BLOCKED** (§9).
5. No live browser, camera, database or deployment verification was performed in this
   review. Everything is offline source/test/build evidence.
6. Login throttling is process-local; distributed rate limiting, MFA and account lockout do
   not exist.
7. Audit immutability is filesystem-permission-based, not cryptographic immutability; the
   chain is tamper-evident, and a full-file rewrite by a writer-attacker is out of scope.
8. Argon2id work factors were never benchmarked on target hardware.
9. Audit retention is at-least-365-days, archive-only, with no automatic deletion.

## 13. Unverified capabilities

* Real PostgreSQL behaviour of `V002`, the FK, the case-insensitive unique index, and the
  advisory-lock admin invariant under actual concurrency.
* The owner SQL script `tools/apply_v002_tier2_test_database.sql` has been statically
  reviewed and unit-tested, never executed.
* Live end-to-end login → dashboard → role-restricted action flow against a running FastAPI
  backend and a real browser.
* Rotation against a filesystem with real archive volumes (soak only, `tmp_path`).
* Backup/restore and disaster-recovery behaviour of the audit journal.

## 14. Remaining blockers

| # | Blocker | Owner action required |
|---|---|---|
| B1 | P5.6 real-PostgreSQL concurrency verification | Authorize a disposable/cluster-pinned run: apply V002 to the dedicated test database, create `attendance_tier2_cleanup`, grant the six least-privilege statements, supply the cleanup DSN, then run `ATTENDANCE_TIER2_OPT_IN=1` |
| B2 | No owner-acceptance record for P5.6/P5.7/P5.8/P5.5.8 | Write the acceptance decision into the repository |
| B3 | P5.7 acceptance exists only in conversation | Record it, or keep "awaiting review" — do not leave it ambiguous |
| B4 | User-management frontend | Separately authorize a proxy allowlist change (4 `/users` routes) *after* B1 |
| B5 | Audit read API/UI | Needs a new milestone; not Phase 5 scope |

## 15. Owner acceptance checklist

Each item is the owner's to tick. Nothing here is pre-ticked.

- [ ] Phase 5 scope and exclusions (§1) match what was authorized.
- [ ] Architecture overview (§2) is accurate.
- [ ] P5.5 evidence (`docs/P5_5_AUDIT_ACCEPTANCE_EVIDENCE.md`) accepted, including the
      P5.5.7.1/P5.5.7.2 reconciliation (§4.1).
- [ ] P5.6 accepted, **with** the disclosed BLOCKED database verification (§9).
- [ ] P5.7 acceptance recorded (currently only conversational).
- [ ] P5.8 accepted; test wrap reviewed as non-weakening.
- [ ] Finding F1 fix accepted (`src/user_service.py`, `src/api_user.py`,
      `tests/test_user_audit_request_id.py`).
- [ ] Findings F2/F3 accepted as disclosed limitations.
- [ ] Product limitations 1–3 (§12) accepted as out of Phase 5 scope.
- [ ] Blocker B1 scheduled; B4 deferred until after B1.
- [ ] Test totals in §10 reproduced or accepted.
- [ ] Final Phase 5 acceptance recorded by the owner.

## 16. Documentation reconciliation

Every inconsistency found while reconciling the Phase 5 documents, and what was done
about it. History is corrected by appended notes, never by rewriting earlier records.

| # | Inconsistency | Resolution |
|---|---|---|
| 1 | `tests/test_audit_exclusion.py` is documented as **50 tests** in `docs/P5_5_AUDIT_ACCEPTANCE_EVIDENCE.md` (§C.1, §D, §H, §I), `docs/PHASE5_IMPLEMENTATION_PLAN.md` §9.7/§9.9 and `docs/AUDIT_JOURNAL_DESIGN.md` §13, but the committed file collects **52**. | The two JSON-parsed value-scanner regressions (`test_scan_values_detects_a_sentinel_inside_an_escaped_json_string`, `test_scan_values_does_not_flag_a_sentinel_missing_from_values`) landed after the count lines were written. Live claims now say **52**; §D.2 of the evidence pack records the correction and the re-measured 15-file total (783 passed, 1 skipped). `CHANGELOG.md` and `DEVELOPMENT_STATUS.md` Step 71 keep "50" as historical step records, consistent with that pack's §F. |
| 2 | `P5.5.7.1` and `P5.5.7.2` both listed against commit `c2871f3` with the same scope, while the plan tracks only `P5.5.7.2`. | Preserved as history; `docs/P5_5_AUDIT_ACCEPTANCE_EVIDENCE.md` §D.0 records that they are **one** delivered milestone, canonical `P5.5.7.2`, and no second implementation or acceptance is invented. |
| 3 | P5.5.6 and P5.5.7.2 are recorded as accepted; the rest of P5.5 is not. | Preserved exactly: partial acceptance stays partial, P5.5.8 stays **PENDING OWNER ACCEPTANCE**, and Phase 5 as a whole stays unaccepted. |
| 4 | P5.6 real-PostgreSQL verification is BLOCKED. | Preserved; §9 above restates the exact prerequisites and this review did not reclassify a skip as a pass. |
| 5 | P5.7 and P5.8 are implemented and committed, but neither document records owner acceptance. | Stated as **implemented, acceptance unrecorded** — implementation status is never used as acceptance (§3). |
| 6 | P5.7's documentation describes 401 handling during session restoration; P5.8's describes a 401 broadcast clearing the signed-in UI. | Not a contradiction: P5.7 established the restoration-failure path; P5.8 extended the same contract to any data request (one `session-events` broadcast, then a safe redirect). §4.3/§4.4 describe the extension rather than treating the earlier text as wrong. |
| 7 | P5.8 does not add user-management or audit screens. | Preserved as a decision, not a gap: §12 explains the proxy allowlist is frozen and no audit read endpoint exists. |
| 8 | `docs/PHASE5_IMPLEMENTATION_PLAN.md` §10 still said "P5.7–P5.9 are not started". | Corrected to "P5.6–P5.8 implemented and committed, P5.9 review complete, none accepted, Phase 6/7 not started". The status header already said the same; the two now agree. |
| 9 | Any document implying Phase 5 is complete or accepted. | None found. The plan header, §10 and §10.2 all state Phase 5 is **not** complete and **not** accepted, as does this document. |
| 10 | Test counts vs. current evidence. | Every count quoted in this document comes from the runs in §10 of this revalidation; the earlier pack's totals are cited only as as-written records. |

## 17. File-change inventory

Only these files differ from `HEAD` (`490ca2a`). Nothing is staged; nothing was committed.

| File | State | Nature |
|---|---|---|
| `src/user_service.py` | modified | F1 fix: `_audit_request_id` resolution (explicit ID → current request ID → `None`), `import uuid` removed, optional `request_id` on all four mutations. |
| `src/api_user.py` | modified | F1 fix: imports `request_id_for`, adds `request: Request` and passes `request_id=request_id_for(request)` on create, role change and deactivate. |
| `tests/test_user_audit_request_id.py` | new | 4 offline regression tests for F1, including a direct `current_request_id() is None` assertion outside a request. |
| `docs/P5_9_PHASE5_FINAL_ACCEPTANCE.md` | new | This acceptance record. |
| `docs/P5_5_AUDIT_ACCEPTANCE_EVIDENCE.md` | modified | §D.0 (P5.5.7.1 ≡ P5.5.7.2), §D.0 request-ID note, §D.2 (sweep count 50 → 52 and the re-measured audit total), I.1. |
| `docs/P5_6_ACCEPTANCE_EVIDENCE.md` | modified | §12: the F1 correction, with the BLOCKED database status left unchanged. |
| `docs/PHASE5_IMPLEMENTATION_PLAN.md` | modified | Status header, §1 bullet, §2 P5.9 row, §9.7/§9.9 sweep count, §10 milestone-status line, §10.2 P5.9 record. |
| `docs/AUDIT_JOURNAL_DESIGN.md` | modified | §13 sweep count 50 → 52 only. |

No other tracked file was touched. No production behaviour outside the two source files above
changed, and no schema, migration, API contract, cookie policy, permission matrix, audit
vocabulary or proxy allowlist was modified.

## 18. Git restrictions compliance

| Rule | Compliance |
|---|---|
| No stage / commit / push / pull / branch / reset / stash / clean / rebase / amend | Not performed. Git was used read-only: `rev-parse`, `status`, `diff`, `log`, `show`. |
| No Git configuration change | Not performed. |
| Read-only inspection allowed | Used for branch/HEAD, `git status --short`, `git diff --stat`, `git log --oneline`, `git show --stat`. |
| Preserve working-tree changes and untracked files | Preserved. The seven P5.9 paths from the previous review are still present and uncommitted; this revalidation only edited them. |

Also observed: no database connection, no migration, no SQL script, no role/`GRANT`/
`REVOKE`, no `.env`/credential/DSN read, no production service started, no real employee,
attendance, audit, biometric or embedding data accessed, and no Phase 6/7 work begun.

---

## Acceptance matrix

| Area | Required outcome | Verdict | Note |
|---|---|---|---|
| Authentication | Source and tests reviewed | **PASS** | §5 |
| Backend RBAC | Permission enforcement reviewed | **PASS** | §6 |
| Frontend authentication | P5.7 reviewed | **PASS** | §4.3, §5 |
| Frontend authorization | P5.8 reviewed | **PASS** | §4.4, §6 |
| Audit journal | Integrity and failure behavior reviewed | **PASS** | §8 |
| Audit coverage | Mandatory mutation events reviewed | **PASS** | F1 fixed; F2/F3 disclosed |
| Rotation and retention | Manifest and recovery reviewed | **PASS** | §8 |
| User administration | Service, API, provisioning reviewed | **PASS** | §4.2; defect F1 fixed |
| Admin invariant | Locking and concurrency evidence reviewed | **BLOCKED** | offline logic reviewed; real-PostgreSQL run blocked (B1) |
| Database safety | Guard and test-target restrictions reviewed | **PASS** | `src/test_database_guard.py`, fail-closed identity/read-only checks |
| Integrated regression | Relevant suites pass | **PASS** | §10 |
| Documentation | Consistent and accurate | **PASS** | this document + reconciled milestone docs |
| Security limitations | Explicitly documented | **PASS** | §12–§14 |

**No unresolved critical or high-severity security defect.** One medium-severity
traceability defect (F1) was found and fixed with regression coverage; two low-severity
limitations (F2, F3) are disclosed. No failing test is unreported. No document claims a
database or live verification that did not happen.

**Recommendation: READY FOR OWNER ACCEPTANCE**, with **B1 (PostgreSQL concurrency)** and
**B2–B3 (acceptance records)** explicitly open. This document does **not** accept Phase 5.

---

## Appendix — source reviewed in this milestone

Backend: `src/authz.py`, `src/api_auth.py`, `src/api_cookies.py`, `src/api_guard.py`,
`src/api_request_context.py`, `src/api_login.py`, `src/api_logout.py`, `src/api_me.py`,
`src/api_user.py`, `src/api_audit.py`, `src/api.py`, `src/audit_schema.py`,
`src/audit_service.py`, `src/audit_writer.py`, `src/audit_manifest.py`,
`src/audit_manifest_store.py`, `src/audit_archive.py`, `src/audit_rotation.py`,
`src/audit_layout.py`, `src/audit_locking.py`, `src/user.py`, `src/user_session.py`,
`src/user_repository.py`, `src/user_service.py`, `src/user_provision.py`,
`src/session_service.py`, `src/session_token.py`, `src/password.py`,
`src/login_throttle.py`, `src/origin.py`, `src/test_database_guard.py`,
`src/schema_contract.py`, `migrations/V002__security.sql` (read-only inspection),
`tools/audit_verify.py`, `tools/audit_rotate.py`.

Frontend: `src/lib/server/proxy.ts`, `src/lib/server/handler.ts`,
`src/lib/api/client.ts`, `src/lib/api/auth.ts`, `src/lib/api/errors.ts`,
`src/lib/api/types.ts`, `src/lib/auth/AuthProvider.tsx`,
`src/lib/auth/useAuthorization.ts`, `src/lib/auth/session-events.ts`,
`src/lib/auth/paths.ts`, `src/lib/authz/permissions.ts`, `src/lib/navigation.ts`,
`src/components/auth/{RequireAuth,RequirePermission,AuthScreens,LoginForm,UserMenu}.tsx`,
`src/components/layout/{Sidebar,MobileNavigation}.tsx`,
`src/components/employees/*`, `src/components/attendance/*`,
`src/app/(dashboard)/{overview,employees,attendance}/page.tsx`.

Tests reviewed or run: `tests/test_user_audit_request_id.py`,
`tests/test_user_service.py`, `tests/test_api_user.py`, `tests/test_api_audit.py`,
`tests/test_audit_*.py`, `tests/test_authz*.py`, `tests/test_api_auth.py`,
`tests/test_api_login.py`, `tests/test_api_logout.py`,
`tests/test_tier2_user_concurrency.py`, `tests/test_test_database_guard.py`,
`frontend/tests/{permissions,role-navigation,role-actions,authz-errors}.test.*`,
`frontend/tests/proxy.test.ts`, `frontend/tests/auth-*.test.*`,
`frontend/tests/employees-*.test.*`, `frontend/tests/attendance-*.test.*`.

Documents: `docs/PHASE5_IMPLEMENTATION_PLAN.md`,
`docs/P5_5_AUDIT_ACCEPTANCE_EVIDENCE.md`, `docs/P5_6_ACCEPTANCE_EVIDENCE.md`,
`docs/P5_7_FRONTEND_AUTHENTICATION.md`, `docs/P5_8_ROLE_AWARE_UI_SECURITY.md`,
`docs/AUDIT_JOURNAL_DESIGN.md`.
