# Phase 5 — Authentication, Accounts and Role-Based Access

**Status: in progress — P5.4 implemented offline through sub-milestone P5.4.10
(no live PostgreSQL run). P5.4.10 is implemented and awaiting owner review;
P5.5 is not started. Phase 5 is not complete.**

Phase 5 adds operator accounts, sessions and role-based access to the
attendance system. It is delivered as small, separately reviewable steps. This
document records the phase plan, the decisions that shape it, and what is
actually implemented versus deferred.

## 1. Scope

Phase 5 covers:

* the identity schema (users and sessions) and its persistence layer;
* password hashing and credential verification;
* session issuance, validation, refresh and revocation;
* login/logout endpoints, `/auth/me` and an authentication dependency;
* the same-origin BFF transport and process-local login throttling;
* role-based authorization for the existing attendance and employee APIs;
* an append-only audit journal for security events (P5.5, not started);
* user administration with secure first-Admin provisioning (P5.6, not started);
* the frontend sign-in flow and role-aware UI (P5.7–P5.8, not started);
* the final Phase 5 security review and acceptance (P5.9, not started).

It does **not** change employee or attendance-journal semantics, does not add an
audit **table** (P5.5's journal is a separate append-only JSONL file, not a
relational table), and does not introduce biometric storage of any kind.

## 2. Milestones

| Milestone | Scope | Status |
| --- | --- | --- |
| P5.1 | Phase 5 inspection, gap analysis and approved decisions | Completed (review, no code) |
| P5.2 | Identity data model and persistence: `V002__security.sql`, user/session models and repositories, offline tests | Completed (offline) |
| P5.3 | Authentication primitives: Argon2id hashing, opaque session tokens, session lifecycle | Completed (offline) |
| P5.4 | Authentication API: session service integration, login/logout, `/auth/me`, BFF, login throttling, backend RBAC (sub-milestones P5.4.1–P5.4.10, §8) | P5.4.1–P5.4.9 completed offline and accepted (committed); P5.4.10 implemented offline, **awaiting owner review** (unstaged, uncommitted) |
| P5.5 | Audit journal and security-event auditing (§9) | Not started |
| P5.6 | User administration and secure first-Admin provisioning (§10) | Not started |
| P5.7 | Frontend authentication and session integration (§10) | Not started |
| P5.8 | Role-aware UI and frontend security hardening (§10) | Not started |
| P5.9 | Final security regression, integration review and Phase 5 acceptance (§10) | Not started |

Each milestone is gated on owner review of the previous one. No milestone
starts early, and no milestone may require a live database or secrets without a
separately authorized run.

**Numbering reconciliation (2026-11-14).** This table previously described P5.4
as the session service, P5.5 as the login/logout endpoints and dependency, P5.6
as role-based authorization and P5.7 as the frontend plus acceptance, while §8
listed RBAC, audit, user management, frontend authentication and the security
review together under "P5.4.10+". The implementation did not follow that
split: P5.4 delivered the session service, login, logout, `/auth/me`, the BFF
transport, throttling **and** the backend authorization foundation, reviewed as
sub-milestones P5.4.1–P5.4.10 in §8. The rows above are the reconciled
top-level structure and the sections below it preserve the historical records
unchanged. **There is no P5.4.11**: P5.4.10 closes the P5.4 sub-milestone
sequence, and the work once grouped under "P5.4.10+" is now split into P5.5
(audit), P5.6 (user administration), P5.7 (frontend authentication), P5.8
(role-aware UI and hardening) and P5.9 (final security review).

## 3. Approved decisions carried into Phase 5

* **Roles**: exactly `Admin`, `HR Manager`, `Operator`, `Auditor`, enforced by a
  database `CHECK` and mirrored by a Python enum. No role hierarchy or
  inheritance; permissions are decided per endpoint in the P5.4.10
  authorization policy (`src/authz.py`, §8).
* **Username identity**: a username is displayed as entered but compared
  case-insensitively through a stored `username_key`. Normalisation is
  NFKC → trim → collapse internal whitespace; the key is the casefold of the
  normalised display value.
* **Timestamps are application-authored.** The schema declares no database
  defaults for `created_at`/`updated_at`/`issued_at`/`expires_at`/
  `absolute_expires_at`/`last_seen_at`, so the application clock is the single
  source of time and tests remain deterministic.
* **Session tokens are stored as hashes only** (`session_token_hash`,
  `CHAR(64)`, primary key). The plaintext token never reaches the database, the
  logs or an exception message. Hashing/session issuance itself is P5.4.
* **Sessions support two expiry bounds**: a sliding `expires_at` and an
  immutable `absolute_expires_at`, plus `revoked_at` for explicit revocation
  and `last_seen_at` for activity tracking.
* **No audit table** for Phase 5. Operational logging stays structured and
  must never include credentials or session material.
* **No changes to `employee` or the attendance journal.**

## 4. P5.2 — what this milestone delivered

### 4.1 Schema (migration V002)

`migrations/V002__security.sql` creates two tables in one transaction:

* `app_user(user_id text PK, username text NOT NULL, username_key text NOT NULL
  UNIQUE, password_hash text NOT NULL, role text NOT NULL CHECK (role IN
  ('Admin','HR Manager','Operator','Auditor')), is_active boolean NOT NULL
  DEFAULT TRUE, created_at timestamptz NOT NULL, updated_at timestamptz NOT
  NULL)`. The unique index on `username_key` is the case-insensitive identity
  constraint; the primary key is the opaque `user_id` the application mints.
* `user_session(session_token_hash character(64) PK, user_id text NOT NULL
  REFERENCES app_user(user_id), issued_at timestamptz NOT NULL, expires_at
  timestamptz NOT NULL, absolute_expires_at timestamptz NOT NULL, revoked_at
  timestamptz NULL, last_seen_at timestamptz NOT NULL)` — plus an index on
  `user_id` (revoke-all and per-user lookups) and an index on `expires_at`
  (expiry sweeps). The foreign key deliberately has no `ON DELETE CASCADE`:
  accounts are deactivated, not deleted, so session rows are never silently
  removed.

V001 is untouched. The migration-set digest changed and is pinned in the schema
contract and its tests.

### 4.2 Models

* `src/user.py`: `Role` enum, `User`, `normalize_username`,
  `username_key_for`, `UserValidationError`. Validation rejects empty,
  too-short (< 3) and over-long (> 64) usernames, control characters, and a
  supplied key that does not equal the derived key. `password_hash` is
  excluded from `repr` so it cannot leak through logging.
* `src/user_session.py`: `UserSession`, `is_valid_session_token_hash`,
  `SessionValidationError`. A token hash must be exactly 64 lowercase hex
  characters.
* `src/config.py`: `UserPaginationConfig` and `USER_PAGINATION`
  (default page size 50, hard maximum 100) so list reads are always bounded.

### 4.3 Repositories

* `src/user_repository.py` — `create`, `find_by_username`, `find_by_id`,
  bounded `list`, `update_role`, `update_active`, with `UserAlreadyExists` and
  `UserNotFound`.
* `src/session_repository.py` — `insert`, `find`, `touch`, `revoke`,
  `revoke_all_for_user`, with `SessionAlreadyExists`, `SessionNotFound` and
  `SessionUserNotFound`.
* `src/db_security_repository.py` — `open_user_repository` /
  `open_session_repository`: one connection per operation, commit on success,
  rollback on failure, always closed.

Every statement is parameterised; no identifier, role, hash or limit is
interpolated into SQL. Repositories contain no password hashing, no session
issuance, no auth dependency and no endpoint code — that is P5.3 onward.

### 4.4 Schema-contract integration

`src/schema_contract.py` now declares `app_user` and `user_session`,
`CONTRACT_MIGRATION_VERSION = 2`, and a required-table order of `employee`,
`app_user`, `user_session`, `schema_version`.

**Documented limitation:** the contract can express tables, columns and primary
keys only. Unique constraints, foreign keys, the role `CHECK` and secondary
indexes cannot be declared there, so `tests/test_migration_v002.py` asserts them
against the migration text instead. This is a limitation of the contract, not a
weakened gate.

### 4.5 Tests

New offline suites (fake driver, no socket, no real database):

* `tests/test_user.py`, `tests/test_user_session.py` — models, normalisation,
  roles, active flag.
* `tests/test_user_repository.py`, `tests/test_session_repository.py` —
  create/find/list/update/revoke behaviour, missing rows, duplicate keys,
  parameterisation.
* `tests/test_db_security_repository.py` — connection scope, commit/rollback.
* `tests/test_migration_v002.py` — V002 structure, ordering, DDL, indexes.
* `tests/test_schema_contract.py`, `tests/test_physical_schema.py`,
  `tests/test_negative_schema_scenarios.py`,
  `tests/test_provision_negative_schema_cases.py`,
  `tests/test_api_physical_gate.py`, `tests/test_startup_schema.py` and the
  migration executor/CLI suites — updated from a one-migration to a
  two-migration world with four contracted tables.

### 4.6 Deliberately not done in P5.2

* No password hashing, no Argon2 (or any dependency) installed.
* No session issuance, no token generation, no cookie/header handling.
* No login/logout endpoint, no authentication dependency, no RBAC.
* No frontend change, no API-contract change.
* No `.env` file, no secret, no database connection, no SQL execution, no live
  PostgreSQL verification.

## 5. What P5.2 did **not** verify

Everything above is verified offline only. The migration has **not** been
applied to a PostgreSQL instance, so its real-plan behaviour, the foreign key's
actual presence, index usage and the case-insensitive uniqueness under real
collation are unverified. The repositories have **not** been exercised against a
live database or a real connection pool. This is the same limitation recorded
for V001, and it is closed only by a separately authorized read-only or
disposable-database run.

## 6. P5.3 — what this milestone delivered

### 6.1 Approved configuration (`src/config.py`)

`PasswordHashingConfig` / `PASSWORD_HASHING`: Argon2id, memory cost 19,456 KiB,
time cost 2, parallelism 1, salt 16 bytes, hash 32 bytes, minimum password
length 8 characters, defensive maximum 1,024 characters (rejection, never
truncation). `SessionPolicyConfig` / `SESSION_POLICY`: 8-hour idle timeout,
12-hour absolute lifetime, 256-bit token entropy. Both are frozen dataclasses
that refuse values below the approved floors; the idle timeout may not exceed
the absolute lifetime.

### 6.2 Password service (`src/password.py`)

`PasswordService.hash_password` / `verify_password` / `needs_rehash`.
Passwords are used verbatim — never trimmed, folded or NFKC-normalised. The
minimum length applies at creation only, so a shorter legacy secret simply
fails to match. Verification returns `False` only for a genuine mismatch;
malformed, non-Argon2id (e.g. Argon2i) or over-budget stored hashes raise
`InvalidPasswordHash`, so verification can never accept what it could not
check. Bounds on type, version 19, memory, iterations, lanes, salt and derived
key length protect against a tampered hash requesting gigabytes of memory. The
encoded result is re-parsed before it is returned.

### 6.3 Token service (`src/session_token.py`)

`generate_session_token` produces 32 bytes from `secrets` (OS CSPRNG) and
validates the result before returning; `session_token_digest` returns the
lowercase SHA-256 hex digest accepted by `CHAR(64)`; `is_valid_session_token`
is a strict structural check (43 base64url characters decoding to exactly 32
bytes) run before any repository call. Only the digest is persisted.

### 6.4 Session lifecycle (`src/session_service.py`, `src/clock.py`)

`SessionService.issue_session` / `validate_session` / `revoke_session` /
`revoke_all_for_user` over the P5.2 repositories, with an injectable clock
(`src.clock.utc_now` by default). Issuance refuses unknown or inactive
accounts before writing. Validation checks structure (no I/O), existence,
clock sanity, revocation, absolute cap, idle deadline, then account existence
and activity, and slides `expires_at` to `min(now + 8h, absolute_expires_at)`
— activity can never extend the cap. Concurrent revocation fails closed.
Every rejection is a typed `AuthenticationError` subtype with a generic
message; repository failures propagate.

### 6.5 Deliberately not done in P5.3

No login/logout/me endpoint, no cookies or CSRF, no throttling, no RBAC, no
audit logging, no user administration, no frontend authentication, no
deployment change. No migration was executed and no database was contacted.

### 6.6 Verification status

Offline only: 90 focused tests (`tests/test_password.py`,
`tests/test_session_token.py`, `tests/test_session_service.py`) plus the full
suite. The services have not been exercised against PostgreSQL; the argon2
work factor was not benchmarked on target hardware.

## 7. Next step

P5.4.10 (backend RBAC foundation and business-route protection) is implemented
and awaiting owner review; once accepted and committed, the next implementation
milestone is **P5.5 — audit journal and security-event auditing** (§9). P5.5 is
not started, and no P5.5 code, schema or test exists in the tree.

## 8. P5.4 authentication API — sub-milestone status

P5.4 is delivered in separately reviewed sub-milestones. Every row up to
P5.4.9 is committed and accepted; P5.4.10 exists as unstaged, uncommitted
working-tree changes awaiting owner review. Nothing beyond these rows exists in
the tree.

| Sub-milestone | Scope | Status |
| --- | --- | --- |
| P5.4.1 | Read-only inspection of the P5.3 primitives, backend, proxy and tests | Completed (review, no code) |
| P5.4.2 | Authentication configuration (session cookie, environment Secure rule, login throttle bounds) | Completed (offline) |
| P5.4.3 | Request ID and correlation infrastructure (server-generated UUID, `X-Request-ID`, context access, log correlation) | Completed (offline) |
| P5.4.4 | Session-cookie utilities and cookie-only authentication dependency (`set_session_cookie`/`clear_session_cookie`, server-side 401 resolution, 503 preserved) | Completed (offline) |
| P5.4.4.1 | Principal hardening: credential-free immutable `AuthenticatedPrincipal`, serialization tests, authentication edge-case tests | Completed (offline) |
| P5.4.5 | `POST /auth/login` (password verification, dummy hash, rehash, session issuance, trusted-origin validation, proxy Origin forwarding) | Completed (offline) |
| P5.4.6 | `POST /auth/logout` (cookie-only token, idempotent 204, cookie clearing) | Completed (offline) |
| P5.4.7 | `GET /auth/me` (session-derived identity, `no-store`, credential-free body) | Completed (offline) |
| P5.4.8 | Authentication BFF (one allowlist/transport at `/api/v1/auth/...`, `Origin` forwarding, `Set-Cookie` playback) | Completed (offline) |
| P5.4.9 | Process-local fixed-window login throttle (5 failures / 15 min per normalized username, 429 + `Retry-After`) | Completed (offline) |
| P5.4.10 | Backend RBAC foundation and business-route protection (`src/authz.py`, eight permissions, four-role matrix, 401/403 guard on the seven business routes) | Implemented (offline), **awaiting owner review** (unstaged, uncommitted) |

P5.4.10 is the backend authorization foundation that an earlier revision of
this section grouped into the broader "P5.4.10+" row together with audit, user
administration, frontend authentication and the final security review. That row
is retired rather than renumbered: there is no P5.4.11. The remaining scope is
defined as P5.5–P5.9 in §9–§10, and it is not started.

P5.4.2 adds configuration only (`src/config.py`, `SESSION_COOKIE`,
`SECURE_COOKIE_ENVIRONMENTS`, `LOGIN_THROTTLE`, `AppSettings.session_cookie_secure`).
The approved values are `va_session` on `/api/v1`, `HttpOnly`, `SameSite=Lax`,
no `Domain`, `Max-Age` 43,200 s, `Secure` in staging/production only, 8-hour
idle timeout, 12-hour absolute lifetime, and a 5-attempt / 15-minute login
throttle keyed by normalized username with no permanent lockout. No switch
exists to trust client request IDs, trust `X-Forwarded-For`, disable CSRF
validation in staging/production, or lock an account permanently.

P5.4.3 adds the request-ID middleware: `va_session`-independent, one canonical
UUIDv4 per HTTP request, `X-Request-ID` on every response including the
catch-all 500, and request-scoped `scope["state"]`/`ContextVar` access.

P5.4.4 adds infrastructure only: `src/api_cookies.py`
(`set_session_cookie`/`clear_session_cookie`, all attributes from
`SESSION_COOKIE` and `AppSettings.session_cookie_secure`, no `Domain`) and
`src/api_auth.py` (`AuthenticatedPrincipal`, `AuthenticationRequired`,
`make_session_dependency`, `build_session_service`). The dependency reads the
`va_session` cookie only, delegates validation to the existing `SessionService`,
and maps every authentication outcome to one sanitized `401
AUTHENTICATION_REQUIRED`; `DatabaseUnavailableError` still reaches the existing
503 handler. **No route uses either module yet** — the eight business endpoints
remain unauthenticated, and login/logout/`/me`, throttling, CSRF, RBAC, audit
logging and first-admin provisioning are still absent.

P5.4.4.1 hardens that principal: it now carries only `user_id`, `username`,
`role` and `is_active`, projected from the validated session by
`AuthenticatedPrincipal.from_session`, so no `User`/`UserSession` object (and
therefore no `password_hash` or `session_token_hash`) is retained; `repr()` and
`jsonable_encoder()` are covered by a regression test. It also adds regression
tests for an empty `va_session` cookie, the absence of `Set-Cookie` on every
authentication refusal, and a non-authentication service fault (`ClockError`)
remaining a sanitized 500 rather than a 401.

P5.4.5 adds `POST /api/v1/auth/login` (`src/api_login.py`): username lookup via
`find_by_username`, one real Argon2id verification, `DUMMY_PASSWORD_HASH` for
unknown accounts, `needs_rehash` → `update_password_hash` before issuance,
`issue_session` plus the approved `va_session` cookie, and a credential-free 200
body. Login requires exactly one `Origin` matching the exact allowlist:
`TRUSTED_ORIGINS` defaults to development `http://localhost:3000` and an empty
(fail-closed) tuple in testing/staging/production; deployments supply their
exact origins through `ATTENDANCE_TRUSTED_ORIGINS`, tests inject
`https://attendance.test.invalid` through `AppSettings.login_trusted_origins`,
and every entry is validated at construction. `src/origin.py` parses strictly
(refusing userinfo, paths, query/fragment — including a trailing `?` or `#` —
invalid ports and IPv6 zone identifiers) and `403 ORIGIN_NOT_ALLOWED` is
sanitized. `build_login_service` fails closed on a missing/blank DSN before
importing the repository layer and shares one user repository between the
lookup and the session service. The
Next.js proxy allowlists the login route alone and forwards its `Origin`, but
still drops `Set-Cookie`, so browser session establishment waits for the BFF
milestone.

P5.4.6 adds `POST /api/v1/auth/logout` (`src/api_logout.py`): the token is read
only from the `va_session` cookie, exactly that session is revoked through the
existing `SessionService.revoke_session`, and the response is `204 No Content`
with `clear_session_cookie`'s approved deletion attributes. Absent, empty,
malformed, unknown, already-revoked and expired sessions are all idempotent
successes with the same cookie clearing and no hint about session existence;
the existing trusted-Origin guard blocks cross-site logout before any
revocation, a database failure stays the sanitized 503 (cookie deliberately
retained so the client can retry) and an unexpected failure the sanitized 500.
P5.4.7 adds `GET /api/v1/auth/me` (`src/api_me.py`): the identity comes from
the existing `make_session_dependency`/`AuthenticatedPrincipal`, the body is the
explicit four-field `MeResponse` (`user_id`, `username`, `role`, `is_active`)
sent with `Cache-Control: no-store`, and every unusable session is the same
sanitized 401 while storage failures remain 503. The route is read-only `GET`,
so it requires no Origin and the login/logout policy is untouched. RBAC
enforcement, business-route protection, session refresh, throttling, audit
logging and frontend login/logout/`me` UI remain unimplemented.

P5.4.8 wires the authentication endpoints through the same-origin BFF. One
strict allowlist and one transport now serve `/api/backend/...` and a new
`/api/v1/auth/...` entry point (the `/api/v1` path is required by the session
cookie's `Path=/api/v1`). The allowlist gains exactly `GET /auth/me` and
`POST /auth/logout` alongside the existing login; login and logout forward
`Origin` and receive the backend's `Set-Cookie` back (per header, never
comma-joined), `/me` and logout forward only `va_session`, upstream statuses
and bodies are preserved, and a 204 stays bodyless. The typed client gains
`login`, `fetchCurrentUser` and `logout` with `credentials: "same-origin"` and
no token handling in JavaScript. Login/logout UI, auth context, protected
routes and RBAC UI remain unimplemented.

P5.4.9 adds `src/login_throttle.py`: a fixed-window limiter keyed by the
**normalized username** (`throttle_key_for` delegates to the same
`src.user.username_key_for` the repository uses, and unkeyable input shares one
isolated bucket so it can never block a real account). The first five failed
attempts per 15-minute window are processed normally; from the fifth failure
on, `LoginService.authenticate` raises `LoginRateLimited` **before** any lookup
or Argon2id verification and the API answers `429 LOGIN_RATE_LIMITED` with a
generic message and a whole-second `Retry-After`. A successful sign-in clears
the key, an expired window starts clean (no permanent lockout), and database or
internal failures abort their reservation instead of counting as a guess. The
component uses `time.monotonic` through an injectable zero-argument clock, a
`threading.Lock` plus a pre-verification slot reservation so concurrent
requests cannot exceed five attempts, and purges expired entries under a key
cap. The application creates exactly one instance and injects it into the
single login service. Counters are **process-local**: multiple application
processes have independent counters, and distributed rate limiting
(Redis/shared storage), IP throttling, CAPTCHA, MFA and account lockout remain
unimplemented.

P5.4.10 adds `src/authz.py`, the single authorization policy, and applies it to
the seven existing business routes. Eight permissions are defined
(`employees:read`, `employees:create`, `employees:update`,
`employees:deactivate`, `attendance:read`, `attendance:checkout`, `audit:read`,
`users:manage`) with one immutable four-role table: Admin holds all eight, HR
Manager holds the two employee reads/writes it needs plus attendance
read/checkout, Operator holds employee read plus attendance read/checkout, and
Auditor holds employee read, attendance read and `audit:read`. Identity comes
only from the validated session principal through the existing P5.4.4
dependency — never from a body, query parameter or header — and the decision is
fail-closed for unknown roles, unknown permissions and inactive principals.
Each business route declares exactly one permission through a single `guard()`
factory (`POST /employees` → `employees:create`, `GET /employees` and
`GET /employees/{id}` → `employees:read`, `PATCH /employees/{id}` →
`employees:update`, `POST /employees/{id}/deactivate` →
`employees:deactivate`, `GET /attendance` → `attendance:read`,
`POST /attendance/{id}/checkout` → `attendance:checkout`). Unauthenticated
requests keep the generic `401 AUTHENTICATION_REQUIRED`; a principal without
the permission gets the sanitized `403 PERMISSION_DENIED` and the endpoint's
business logic never runs; storage failures stay `503` and unexpected faults
`500`. `GET /health` remains anonymous and login/logout/`me` are unchanged and
outside business RBAC. `audit:read` and `users:manage` are policy definitions
only: no audit journal, audit hash chain, user-management API, first-Admin
provisioning, frontend authentication or route guards exist yet.

## 9. P5.5 — Audit journal and security-event auditing (not started)

P5.5 is the next implementation milestone. It is **not started**: no audit
module, journal file, schema, verifier or test exists in the tree, and this
section is planning only.

### 9.1 Objective

Give Phase 5 a durable, tamper-evident record of security-relevant events —
authentication outcomes, authorization denials and (once P5.6 lands) user and
role changes — without touching employee or attendance semantics and without
introducing an audit **table**.

### 9.2 Approved architecture (already decided, not re-opened)

* **Separate, append-only JSONL audit journal** at `data/audit/audit.jsonl`,
  independent of the attendance journal and of PostgreSQL. It is never written
  through the attendance store, never interleaved with attendance records and
  never exposed as an API collection in P5.5.
* **Versioned record schema.** Every record carries `schema_version` so a
  reader can refuse or migrate an unknown format instead of guessing.
* **Hash-chain integrity.** Each record embeds the digest of the previous
  record (a defined genesis value starts the chain) and its own digest over a
  canonical serialization, so an edited, deleted or reordered record breaks
  verification. The chain is **tamper-evident, not tamper-proof**: an attacker
  with write access can rewrite the whole file, which the documentation must
  state plainly rather than overclaim.
* **Application-generated timestamps** from the existing clock seam (no
  database or filesystem default), so behaviour is deterministic under a fake
  clock.
* **Actor identity derived from `AuthenticatedPrincipal`** (never from the
  request body, query or a header): `actor_user_id` and `actor_role`. Events
  that precede authentication (e.g. a failed login) use an explicit system
  actor with no user identity.
* **Action, target type and target identifier** for every record, drawn from a
  closed set of action names and target types.
* **Outcome**: exactly one of `success`, `denied`, `error`, `uncertain`.
  `uncertain` exists for the case where the state change may have happened but
  could not be confirmed — it must never be replaced by `success`.
* **Structured reason code** from a closed enum, so consumers never parse
  prose.
* **Request ID correlation** using the existing server-generated request ID, so
  an audit record can be matched to the HTTP response and structured logs.
* **Safe metadata only**: a small, allowlisted set of non-sensitive fields
  (e.g. endpoint, method, outcome detail code). Never request bodies.
* **Forbidden material, without exception**: passwords, password hashes,
  session tokens, cookies, token digests, biometric information, face
  embeddings, photographs, and personal data beyond the actor identifiers and
  the audited target ID.
* **Retention**: 365 days is the approved retention policy. P5.5 documents it;
  automated archival/purge is deferred and, when added, must never silently
  break the chain.
* **Verification CLI** for journal integrity: read-only, offline, reports chain
  validity, record count, sequence range and the position of the first broken
  link, and exits non-zero on any failure.

### 9.3 Approved failure policy

* **Mutations fail closed.** A protected business mutation or a P5.6 user/role
  change must not report success unless its required audit record was written.
  Where the state change is already durable and cannot be rolled back (the
  attendance journal append), a failed audit write is surfaced as a sanitized
  `503` with outcome `uncertain` — never as success, and never silently
  dropped.
* **User and role changes must never succeed unaudited** (P5.6 consumes this
  rule).
* **Read and login-attempt auditing is best-effort.** A failed write does not
  block the response, but the failure must be **explicitly surfaced** (stable
  structured log entry and documented behaviour), never swallowed.
* **No sensitive material in any failure path**: audit errors, sanitized 503
  bodies and logs stay credential-free.

### 9.4 Boundaries — P5.5 must not

* change employee or attendance-journal semantics;
* add an attendance or embedding table, or move biometric data into
  PostgreSQL;
* implement user-administration endpoints (P5.6);
* implement frontend authentication UI (P5.7);
* introduce distributed rate limiting, CAPTCHA, MFA or account lockout;
* change the approved session-cookie or password-hashing policy;
* execute migrations or connect to a live database without separate owner
  authorization.

### 9.5 Required test plan

* **Unit** — canonical serialization and digest stability, genesis handling,
  schema/version validation, closed action/reason/outcome sets, actor
  projection from `AuthenticatedPrincipal`, metadata allowlist and redaction
  (asserting credential and biometric keys are rejected), sequence
  monotonicity, injected clock.
* **Integration** — one record per protected endpoint outcome (success, denied,
  error), 401/403 paths, login success/failure, logout, `/me` (if audited),
  request-ID correlation, and an end-to-end scan proving no credential, token,
  cookie, hash or biometric value appears in any record.
* **Concurrency** — parallel writers produce contiguous, duplicate-free
  sequence numbers and a chain that still verifies; the single-writer/locking
  assumption is stated explicitly, and any multi-process limitation is
  documented and tested as a limitation rather than claimed as safe.
* **Crash recovery** — empty journal, missing directory, truncated final line,
  torn/partial write, and a journal containing a valid prefix plus garbage:
  reads and verification must fail closed or ignore only an incomplete trailing
  record, never treat it as success.
* **Hash-chain verification** — detecting a modified field, a deleted record, a
  reordered pair and a forged digest; CLI exit codes and output.
* **Audit-failure** — an injected write failure makes a mutation fail closed
  with a sanitized 503; a best-effort read/login audit failure leaves the
  response unchanged but is surfaced; no failure path leaks credentials.
* All tests use fake clocks and temporary directories; no real sleeps, no
  PostgreSQL and no biometric data.

### 9.6 Acceptance criteria

1. The journal exists at the documented path, is append-only JSONL, versioned
   and hash-chained, with a verification CLI that passes on a healthy journal
   and exits non-zero for every tampering case above.
2. Every protected mutation emits exactly one record before success, and a
   failed write fails closed; user/role changes (P5.6) inherit that rule.
3. Read and login-attempt auditing is best-effort with failures explicitly
   surfaced.
4. Records carry actor, actor role, action, target, outcome, reason code,
   request ID and allowlisted metadata, and never any forbidden material —
   proven by tests.
5. Concurrency, crash-recovery and failure-policy tests pass deterministically.
6. Documentation states the 365-day retention policy, the tamper-evident (not
   tamper-proof) property and the offline verification limits.
7. No change to employee/attendance semantics, no migration, no biometric
   storage, no user-administration endpoint and no frontend change.

## 10. P5.6–P5.9 — subsequent milestones (not started)

None of these is started, and none may be pulled forward without a separate
owner instruction.

**P5.6 — User administration and secure first-Admin provisioning.** Admin-only
user management (`users:manage`): create, list, change role and deactivate
users, with a documented one-time first-Admin provisioning path that has no
default credentials and is itself audited. Last-active-Admin protection, no
self-role change and no self-deactivation. Audit integration is required (P5.5
failure policy applies), and no audit table is added.

**P5.7 — Frontend authentication and session integration.** Login and logout
UI, session retrieval through the existing `/auth/me`, same-origin BFF usage,
cookie-based session handling (HttpOnly, never readable by JavaScript),
authentication-required states and redirects. No token storage in JavaScript
and no role-aware UI yet.

**P5.8 — Role-aware UI and frontend security hardening.** Permission-aware
navigation and actions that reflect the P5.4.10 policy while the backend stays
the sole authorization authority, session-expiry handling, correct 401 versus
403 behaviour in the client, security headers and frontend hardening. An audit
read view for the Auditor role only if and when a P5.5 API design is approved
(`audit:read` currently guards no endpoint).

**P5.9 — Final security regression and Phase 5 acceptance.** Full authorization
matrix regression, authentication and session-lifecycle regression, audit
integrity and failure-policy tests, user-management security tests, frontend
integration regression, documentation and known-limitations review, and the
final owner acceptance. Phase 5 is **not** complete before this milestone is
accepted.
