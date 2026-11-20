# Phase 5 — Authentication, Accounts and Role-Based Access

**Status: in progress — P5.4 completed offline through sub-milestone P5.4.10 and
accepted (committed in `b0583df`; no live PostgreSQL run). The P5.5.1 audit
journal design is approved; P5.5.2 (schema, canonical JSON, SHA-256 hashing and
chain verification) and P5.5.3 (durable, verified, poisoning journal writer) are
implemented and awaiting review, P5.5.4 (the policy-checking audit service) is
implemented in `src/audit_service.py`, P5.5.5 (route integration and restricted
mode) in `src/api_audit.py`/`src/api.py`, and P5.5.6 (the read-only active-
journal verifier CLI) in `tools/audit_verify.py`. **P5.5.6 is accepted by the
owner (committed in `87c73e5`) and P5.5.7.2 is accepted; P5.5.2–P5.5.5 await
review.** **P5.5.7 is implemented and awaits independent review**: manifest
persistence (`src/audit_manifest_store.py`), archive/history verification
(`src/audit_archive.py`), operator rotation, crash recovery and retention
recording (`src/audit_rotation.py`, `tools/audit_rotate.py`), manifest-aware
writer startup (`src/audit_writer.py`) and the three-mode read-only verifier
(`tools/audit_verify.py`). **P5.5.8 is implemented and awaiting review** (exclusion
sweep, failure/recovery regression, bounded concurrency/rotation soak,
documentation reconciliation and the evidence pack). **P5.6 (user administration and
first-Admin provisioning) is implemented and awaits independent review**: the
service/repository invariants, the four Admin-only endpoints, the CLI
provisioner and the offline test suites exist, but the PostgreSQL concurrency
suite is **blocked** because the dedicated test database does not contain the
`app_user` table (migrations were deliberately not executed). **P5.7 and P5.8 are
implemented and committed** (`246a2f6`, `490ca2a`). **P5.9 is implemented and its
integrated review is complete, awaiting independent review and owner
acceptance** — one audit-correlation defect was found and fixed, and the
real-PostgreSQL concurrency verification remains blocked
(`docs/P5_9_PHASE5_FINAL_ACCEPTANCE.md`). Phase 5 is **not** complete and **not**
accepted; no milestone's "implemented" status implies acceptance.**

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
* user administration with secure first-Admin provisioning (P5.6, implemented
  and awaiting review);
* the frontend sign-in flow (P5.7, implemented) and role-aware UI (P5.8,
  implemented and awaiting review);
* the final Phase 5 security review and acceptance (P5.9, review complete and
  awaiting owner acceptance).

It does **not** change employee or attendance-journal semantics, does not add an
audit **table** (P5.5's journal is a separate append-only JSONL file, not a
relational table), and does not introduce biometric storage of any kind.

## 2. Milestones

| Milestone | Scope | Status |
| --- | --- | --- |
| P5.1 | Phase 5 inspection, gap analysis and approved decisions | Completed (review, no code) |
| P5.2 | Identity data model and persistence: `V002__security.sql`, user/session models and repositories, offline tests | Completed (offline) |
| P5.3 | Authentication primitives: Argon2id hashing, opaque session tokens, session lifecycle | Completed (offline) |
| P5.4 | Authentication API: session service integration, login/logout, `/auth/me`, BFF, login throttling, backend RBAC (sub-milestones P5.4.1–P5.4.10, §8) | Completed offline through P5.4.10 and accepted (committed in `b0583df`) |
| P5.5 | Audit journal and security-event auditing (§9, `docs/AUDIT_JOURNAL_DESIGN.md`) | P5.5.1 design approved; P5.5.2 primitives, P5.5.3 writer, P5.5.4 service and P5.5.5 route integration **implemented, awaiting review**; P5.5.6 verifier CLI **accepted (committed `87c73e5`)**; P5.5.7.2 manifest schema/integrity **accepted**; P5.5.7 manifest persistence, rotation, recovery, retention and manifest-aware verification **implemented, awaiting review**; P5.5.8 not started |
| P5.6 | User administration and secure first-Admin provisioning (§10) | **Implemented, awaiting independent review**; real-PostgreSQL concurrency scenarios **blocked** (test database lacks `app_user`/`user_session`) |
| P5.7 | Frontend authentication and session integration (§10.2, `docs/P5_7_FRONTEND_AUTHENTICATION.md`) | **Implemented, awaiting review** — login page, session restoration, protected routes, sign-out; offline tests, lint, typecheck and production build green |
| P5.8 | Role-aware UI and frontend security hardening (§10, `docs/P5_8_ROLE_AWARE_UI_SECURITY.md`) | **Implemented, awaiting review** — mirrored permission model, navigation and action gating, access-denied screen, 401/403/503 handling; offline tests, lint, typecheck and production build green. User-administration and audit screens deliberately not built |
| P5.9 | Final security regression, integration review and Phase 5 acceptance (§10) | **Implemented, awaiting independent review and owner acceptance** — review complete, one audit-correlation defect found and fixed, real-PostgreSQL verification still **BLOCKED**; Phase 5 is **not** accepted (`docs/P5_9_PHASE5_FINAL_ACCEPTANCE.md`) |

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

P5.4.10 (backend RBAC foundation and business-route protection) is accepted and
committed in `b0583df`. The owner has approved the **P5.5.1** audit-journal
design (`docs/AUDIT_JOURNAL_DESIGN.md`). **P5.5.2** (`src/audit_schema.py`) and
**P5.5.3** (`src/audit_writer.py`) are implemented and awaiting review: the
schema/hash/chain primitives plus the durable, fully verified, fsync-backed,
poisoning writer, and **P5.5.4** (`src/audit_service.py`) adds the policy-checked
service layer (context binding, matrix enforcement, intent/result helpers,
best-effort wrapper), and **P5.5.5** integrates that service into the FastAPI
application (`src/api_audit.py`, `src/api.py`, `src/api_login.py`,
`src/api_logout.py`, `src/authz.py`) with fail-closed mutations, best-effort
security events and restricted mode, and **P5.5.6** adds the read-only
active-journal verifier CLI (`tools/audit_verify.py`), which the owner has
accepted (committed in `87c73e5`) and **P5.5.7.2** (the manifest schema and
integrity foundation, `src/audit_manifest.py`) is accepted. **P5.5.7** is now
implemented and awaits review: durable manifest persistence, archive and
historical chain verification, operator-controlled rotation
(`tools/audit_rotate.py`), the five documented crash states with explicit
recovery commands, retention recording with anchor-only reporting, manifest-aware
writer startup and the three-mode read-only verifier. The next milestone is
**P5.5.8** (security regression, fault injection, exclusion sweep and Phase 5
acceptance); it has not started.

## 8. P5.4 authentication API — sub-milestone status

P5.4 is delivered in separately reviewed sub-milestones. Every row below is
committed and accepted (P5.4.10 in `b0583df`). Nothing beyond these rows exists
in the tree.

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
| P5.4.10 | Backend RBAC foundation and business-route protection (`src/authz.py`, eight permissions, four-role matrix, 401/403 guard on the seven business routes) | Completed (offline), accepted and committed (`b0583df`) |

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

## 9. P5.5 — Audit journal and security-event auditing (P5.5.7 implemented, awaiting review)

P5.5.1 (design) is approved; P5.5.2 (schema and hash-chain primitives,
`src/audit_schema.py`, 172 tests), P5.5.3 (durable verified writer,
`src/audit_writer.py`, 66 tests), P5.5.4 (policy-checked service,
`src/audit_service.py`, 39 tests) and P5.5.5 (route integration and restricted
mode, 40 tests) are implemented and awaiting review; P5.5.6 (read-only verifier
CLI, 38 tests) is accepted (committed in `87c73e5`). P5.5.7.2 (manifest schema
and integrity foundation, 163 tests, `src/audit_manifest.py`) is accepted.
P5.5.7 (manifest persistence, archive/history verification, operator rotation,
crash recovery, retention recording, manifest-aware writer startup and the
three-mode verifier) is implemented and awaiting review; there is still **no**
audit read API, no deletion, no scheduler and no database audit table. This section summarises the approved design; the
authoritative record is **`docs/AUDIT_JOURNAL_DESIGN.md`** (P5.5.1 design plus
§15 P5.5.2, §17 P5.5.3, §18 P5.5.4 and §19 P5.5.5 implementation notes), which
governs whenever the summary and the design record differ.

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
  state plainly rather than overclaim. `sequence` is global and never resets;
  each segment also carries `segment_sequence` and its first record links to
  the preceding segment's last `record_hash`, so rotation cannot fork the chain.
  A writer must verify the **entire available chain** before accepting any
  append, may not trust a cached tail hash, and no trusted checkpoint is
  introduced in P5.5. Segment identity is the owner-approved `YYYY-MM-NNN`
  allocation identifier (design record §16): unique per segment regardless of
  how many rotations occur in a month, with order proven by sequences, links and
  (from P5.5.7) the manifest, never by label comparison.
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
* **Safe metadata only**: a small, allowlisted set of scalar fields (`endpoint`
  as a route template, `method`, `http_status`, `employee_active_after`,
  `journal_event_id`, and P5.6's `prior_role`/`new_role`). Never raw URLs, query
  strings, request bodies, headers or free text.
* **Forbidden material, without exception**: passwords, password hashes,
  session tokens, cookies, token digests, biometric information, face
  embeddings, photographs, and personal data beyond the actor identifiers and
  the audited target ID.
* **Retention**: audit records are retained for **at least 365 days**. The
  initial implementation is **archive-only** — there is no automatic deletion
  and no deletion schedule, and any future deletion requires a separate owner
  decision. Rotation is operator-controlled (target cadence monthly) and is
  implemented in P5.5.7 while the application is stopped or the writer is
  exclusively locked. A sealed segment is immutable; each new active segment
  links to the preceding segment's final hash, so rotation never resets the
  chain. The manifest is a separate append-only, independently chained index and
  never pretends that removed records were verified.
* **Verification CLI** `tools/audit_verify.py`: read-only and offline (it never
  repairs, rotates or rewrites). It verifies canonical hashes, schemas,
  vocabulary, global and segment sequence continuity, previous-hash links across
  segment boundaries, and manifest/segment agreement, and it reports whether
  coverage is full-chain, segment/manifest continuity, or anchor-only. Exit
  codes: `0` PASS, `1` integrity failure, `2` unavailable or changed during
  verification, `3` usage error, `4` internal error, `5` recovery required —
  the same stable mapping as `docs/AUDIT_JOURNAL_DESIGN.md` §11.

### 9.3 Approved failure policy

* **Mutations fail closed through an intent/result protocol.** Before an
  employee create/update/deactivate or an attendance checkout runs, a durable
  **intent** record (`outcome=uncertain`, `reason_code=AWAITING_RESULT`) must be
  written; if it cannot be written, the mutation is not executed and the API
  answers `503 AUDIT_UNAVAILABLE`. After the business operation a correlated
  **result** record is written. If that fails after the operation may have
  applied, the API answers `503 AUDIT_RESULT_UNCERTAIN`: the operation may have
  applied and its audit result is unknown. No automatic retry and no
  compensating mutation.
* **Three outcome states, never conflated.** A result record is `success` only
  for a confirmed-applied operation, `error` only when evidence proves the
  operation did not take effect (validation, business refusal, unique-constraint
  rejection, connection never opened), and `uncertain` when the final state
  cannot be established (commit failure, connection loss after a statement, or
  any interruption). A database exception is never assumed to mean rollback, and
  an intent record is never treated as success.
* **Login, logout and permission-denial auditing is best-effort.** A failed
  write does not change the existing response; it is surfaced as a sanitized
  structured warning carrying the action, request ID and error class only. No
  audit-failure event is written to the journal that just failed, no global
  login-write throttling is added, and the approved login throttle policy is
  unchanged.
* **Login failures retain no username**: `target_type="account"` with a null
  `target_id`.
* **Bounded records**: ≤ 8192 bytes per record, ≤ 16 allowlisted scalar metadata
  keys and ≤ 1024 bytes of metadata, `target_id` ≤ 256 chars; route templates
  only, never raw URLs, query strings or request bodies.
* **No sensitive material in any failure path**: passwords, hashes, tokens,
  cookies, digests, DSNs, biometric data, request bodies and unnecessary
  personal information never enter a record, a log line or an error response.

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
* **Writer trust and poisoning** — an existing journal is fully verified before
  the first write (never just its tail hash); a failed verification refuses all
  appends; any ambiguous write/flush/fsync failure poisons the writer instance,
  which then refuses every later append until a fresh full verification; a
  torn/malformed tail is never repaired.
* **Crash recovery** — empty journal, missing directory, truncated final line,
  torn/partial write, a valid prefix plus garbage, and writer-restart after
  poisoning: verification must fail closed and never treat damage as success.
* **Hash-chain verification** — detecting a modified field, a deleted record, a
  reordered pair and a forged digest; CLI exit codes and output.
* **Audit-failure** — an injected intent-write failure prevents the mutation
  and returns `503 AUDIT_UNAVAILABLE`; an injected result-write failure after a
  possibly applied mutation returns `503 AUDIT_RESULT_UNCERTAIN`; a best-effort
  login/logout/denial audit failure leaves the response unchanged but is
  surfaced; no failure path leaks credentials.
* **Mutation outcomes** — confirmed-applied, confirmed-not-applied and unknown
  cases each emit the documented result record; a commit-failure simulation is
  recorded as `uncertain`, never as `error`-means-rolled-back.
* All tests use fake clocks and temporary directories; no real sleeps, no
  PostgreSQL and no biometric data.

### 9.6 Acceptance criteria

1. The journal exists at the documented path, is append-only JSONL, versioned
   and hash-chained with a global sequence and segment-local numbering, and the
   read-only CLI passes on a healthy journal and exits non-zero for every
   tampering, torn-tail, sequence or manifest defect above.
2. Every protected mutation writes a durable intent record **before** executing
   and a correlated result record afterwards; no unaudited success is possible,
   and a failed result record yields `503 AUDIT_RESULT_UNCERTAIN` rather than a
   success. User/role changes (P5.6) inherit the same rule.
3. Login, logout and permission-denial auditing is best-effort with failures
   explicitly surfaced, without altering their HTTP responses.
4. Records carry actor, actor role, action, target, outcome, reason code,
   request ID and allowlisted metadata, and never any forbidden material —
   proven by tests.
5. Concurrency, crash-recovery and failure-policy tests pass deterministically.
6. Documentation states the at-least-365-day, archive-only retention policy (no
   automatic deletion), the tamper-evident (not tamper-proof) property, the
   three verification coverage modes and the offline limits.
7. No change to employee/attendance semantics, no migration, no biometric
   storage, no user-administration endpoint and no frontend change.

### 9.7 P5.5 sub-milestones

The authoritative per-milestone scope, exclusions and exit criteria are in
`docs/AUDIT_JOURNAL_DESIGN.md` §13; the sequence is fixed.

| # | Scope | Status |
| --- | --- | --- |
| P5.5.8.2 | Exclusion sweep harness (escape-aware value and key scanner) — **implemented, awaiting review**: `tests/test_audit_exclusion.py` | 52 tests; positive control — done |
| P5.5.8.3 | Failure and recovery regression (runtime poisoning, restricted mode, crash → recover cycles) — **implemented, awaiting review**: `tests/test_audit_failure_regression.py` | 18 tests — done |
| P5.5.8.4 | Bounded concurrency and rotation soak — **implemented, awaiting review**: `tests/test_audit_concurrency_soak.py` | 5 tests: 20 × 50 appends, 12 rotations, month boundary — done |
| P5.5.8.5 | Documentation reconciliation — **implemented, awaiting review** | `git diff --check` — done |
| P5.5.8.6 | Acceptance evidence pack — **prepared, awaiting review**: `docs/P5_5_AUDIT_ACCEPTANCE_EVIDENCE.md` | Evidence complete; acceptance is the owner's decision |

P5.5.8.5 corrects the stale exit-code statements that predated P5.5.7 (the
architecture doc still said "exit codes 0/2/3/4" before this reconciliation;
the implemented contract is 0/1/2/3/4/5 with 5 = recovery required), adds the
concurrency-model boundary to design §9.11, and keeps the scope statements
accurate (P5.5.8 is audit verification and acceptance preparation; P5.5.9 is final
Phase 5 acceptance).

| # | Scope | Prerequisites | Exclusions | Verification / exit |
| --- | --- | --- | --- | --- |
| P5.5.1 | Design and decision record (§9; `docs/AUDIT_JOURNAL_DESIGN.md`) | P5.4.10 accepted | Code | Owner review; no open contradiction |
| P5.5.2 | Audit event schema and hash-chain primitives (canonical JSON, SHA-256, vocabularies, allowlists, genesis) — **implemented, awaiting review**: `src/audit_schema.py`; `YYYY-MM-NNN` segment identity applied (design §16) | P5.5.1 | I/O, writer, HTTP | Unit matrix green (172 tests); no filesystem access — done |
| P5.5.3 | Durable append-only writer: full verification before first write, process + OS lock, fsync, poisoning, typed errors; **no rotation** — **implemented, awaiting review**: `src/audit_writer.py` | P5.5.2 | Rotation, manifest writes, routes | Never appends to unverified journal; ambiguous failures poison — done (66 tests) |
| P5.5.4 | Audit service, actor/request context, intent/result helpers, best-effort wrapper, safe logging — **implemented, awaiting review**: `src/audit_service.py` | P5.5.3 | Route changes | Actor/request-ID propagation; no secret in output — done (40 tests) |
| P5.5.5 | Mandatory mutation and best-effort event integration, `AUDIT_UNAVAILABLE`/`AUDIT_RESULT_UNCERTAIN`, restricted mode — **implemented, awaiting review**: `src/api_audit.py`, `src/api.py`, `src/api_login.py`, `src/api_logout.py`, `src/authz.py` | P5.5.4 | User admin, frontend, new endpoints | No unaudited success; auth/authz behaviour unchanged — done (40 tests) |
| P5.5.6 | Read-only active-journal verifier CLI, exit codes 0/1/2/3/4 (meanings unchanged) — **accepted (committed `87c73e5`)**: `tools/audit_verify.py` | P5.5.3 | Repair, rotation, manifest/archive | Tamper matrix green; journal bytes unchanged; nothing created — done (38 tests) |
| P5.5.7.2 | Manifest schema and integrity foundation: exact §9.2 field set, canonical hashing, manifest chain and continuity, archive-reference validation — **accepted**: `src/audit_manifest.py` | P5.5.6 accepted | Manifest file I/O, sealing, archives, rotation, recovery, CLI | Schema/hash/chain/path matrix; no filesystem access — done (163 tests) |
| P5.5.7 | Retention, archive and operator-controlled rotation; manifest persistence; crash recovery; manifest-aware writer and verifier; runbook — **implemented, awaiting review**: `src/audit_manifest_store.py`, `src/audit_archive.py`, `src/audit_rotation.py`, `src/audit_layout.py`, `src/audit_locking.py`, `tools/audit_rotate.py`, `tools/audit_verify.py` | P5.5.6, P5.5.7.2 | Automatic deletion, automatic rotation, automatic repair | Rotation preserves chain; every crash state requires an explicit command; anchor-only coverage reported honestly — done (190 new tests) |
| P5.5.8 | Security regression, fault injection, exclusion sweep, docs and acceptance | P5.5.1–P5.5.7 | P5.6 work | Full matrix + review; owner acceptance |
| P5.5.8.2 | Exclusion sweep harness (escape-aware value and key scanner) — **implemented, awaiting review**: `tests/test_audit_exclusion.py` | P5.5.8.1 | Production code | 52 tests; positive control — done |
| P5.5.8.3 | Failure and recovery regression (runtime poisoning, restricted mode, crash → recover cycles) — **implemented, awaiting review**: `tests/test_audit_failure_regression.py` | P5.5.8.1 | Production code | 18 tests — done |
| P5.5.8.4 | Bounded concurrency and rotation soak — **implemented, awaiting review**: `tests/test_audit_concurrency_soak.py` | P5.5.8.3 | Multi-process claims | 5 tests — done |
| P5.5.8.5 | Documentation reconciliation — **implemented, awaiting review** | P5.5.8.4 | Production behaviour | `git diff --check` — done |
| P5.5.8.6 | Acceptance evidence pack — **prepared, awaiting review**: `docs/P5_5_AUDIT_ACCEPTANCE_EVIDENCE.md` | P5.5.8.5 | Acceptance itself | Owner review |

## 10. P5.6–P5.9 — subsequent milestones

P5.6–P5.8 are implemented and committed, and P5.9's integrated review is
complete; **none of them is accepted**, and Phase 6/7 is not started and may not
be pulled forward without a separate owner instruction.

### 10.1 P5.6 — User administration and secure first-Admin provisioning (implemented, awaiting review)

**Scope delivered**

* `src/user_service.py` — `UserService` with `list_users`, `create_user`,
  `change_user_role`, `deactivate_user` and `provision_first_admin`. It refuses
  non-Admin actors (defence in depth behind the route guard), self-role-change
  and self-deactivation, hashes passwords with the existing Argon2id service,
  and never returns a hash or credential. `sessions` is a **required**
  constructor argument, so deactivation always calls
  `SessionService.revoke_all_for_user`; there is no "skip revocation" path.
* `src/user_repository.py` — the last-active-Admin invariant is enforced in
  one database transaction, not by a read-then-write in Python. Every operation
  that can change the active-Admin count uses the same private section helper
  (`_begin_admin_invariant_transaction`):

  1. `SET TRANSACTION ISOLATION LEVEL READ COMMITTED` (first statement);
  2. `pg_advisory_xact_lock(_ADMIN_INVARIANT_LOCK_KEY)`;
  3. check the target's current role/active state, then count, then write —
     all in that same transaction.

  Participants: ordinary **Admin creation** (`create` with `role is Admin`),
  **first-Admin provisioning** (`provision_first_admin`, which also refuses
  when an active Admin exists), **demotion**
  (`update_role_with_admin_check`) and **deactivation**
  (`update_active_with_admin_check`). Non-Admin creation and non-Admin updates
  cannot reduce the count and take no lock. The count is only read when the
  target is an active Admin losing that status, so a non-Admin operation is
  never refused merely because one active Admin exists. A unique violation is
  still translated to `UserAlreadyExists`. There is deliberately **no**
  service-side count: a Python pre-check would read a single paginated page
  (`UserRepository.list` defaults to 50 rows) and both refuse safe demotions
  and allow unsafe ones; `UserService` only translates
  `LastAdminProtectionError` into `LastAdminProtectedError`.

  **Isolation is enforced, not assumed.** The advisory lock serialises the
  decision, but the decision reads committed rows. Under `READ COMMITTED` each
  statement takes a fresh snapshot, so the waiter sees the winner's commit.
  Under `REPEATABLE READ`/`SERIALIZABLE` the waiter's snapshot is fixed at its
  first statement and could take a stale decision, so each invariant
  transaction pins `READ COMMITTED` before the lock. This requires the
  documented one-connection-per-operation model (`SET TRANSACTION` is only
  valid before the transaction's first query). Limitations: only cooperating
  application operations are serialised (direct SQL or another writer can
  still break the invariant; there is no database constraint for "at least one
  active Admin"), the single global key serialises invariant operations with
  each other, and correctness is asserted for `READ COMMITTED` only.
* `src/api_user.py` — the four Admin-only endpoints (`GET /api/v1/users`,
  `POST /api/v1/users`, `PATCH /api/v1/users/{user_id}/role`,
  `POST /api/v1/users/{user_id}/deactivate`) guarded by `users:manage` through
  the existing `permission_dependency` → `make_session_dependency` chain. The
  module owns route definitions only: `register_user_routes` receives the
  application's **cached** `get_user_service` and `get_session_service`
  getters, so it reads no private attribute and builds no service, writer or
  journal per request. `build_user_service` mirrors `build_login_service`
  (DSN check before repository/driver imports, no connection at build time).
* `src/api.py` — registers the four routes (inventory `APPROVED_ROUTES` grows
  from 11 to 15), builds **one** `UserService` on first use via
  `_resolve_user_service` over the application's audit subsystem and session
  service, and fails closed with `503 AUDIT_UNAVAILABLE` when the audit
  subsystem is restricted. `create_app` accepts a `user_service` seam for
  offline tests.
* `src/user_provision.py` — the operator-only CLI
  (`python -m src.user_provision`): no HTTP route, no default credentials,
  username/ID from prompts or explicit arguments, password via `getpass` plus
  confirmation, hashed before the single insert, output limited to identity
  fields, exit `1` on refusal/failure and `2` on usage errors. It composes the
  application's own `AuditSubsystem`/`production_audit_builder` and a real
  `SessionService`, so the intent/result protocol and `source=cli` apply.
* **Audit** — every mutation writes a durable intent before the write and a
  correlated result afterwards (`begin_user_create`,
  `begin_user_role_change`, `begin_user_deactivate`,
  `begin_first_admin_provision` + `record_applied` / `record_not_applied` /
  `record_unknown`). An unavailable audit subsystem aborts before the mutation
  (`AUDIT_UNAVAILABLE`); any failure other than the typed, provably-not-applied
  errors is recorded as `DATABASE_OUTCOME_UNKNOWN`; a failed result write after
  a successful mutation surfaces the existing `AUDIT_RESULT_UNCERTAIN`
  contract, and no mutation is retried. A failed session revocation after an
  applied deactivation is recorded as `DATABASE_OUTCOME_UNKNOWN` and answered
  as `503 SESSION_REVOCATION_FAILED`, never as success or a definite failure.
  No password or hash is ever placed in an audit record.
* **No schema change by this milestone.** `migrations/V002__security.sql`
  (P5.2) already defines `app_user` and `user_session` (the latter supports
  user-scoped revocation). P5.6 adds no migration file. The only schema
  tooling it adds is the owner-run, test-database-only
  `tools/apply_v002_tier2_test_database.sql` (its single hard target is
  `visual_attendance_tier2_test`; no application or production target is
  named in it). The only migration attempt was on the disposable tier-2
  database and was refused by privileges
  before any write (see "Blocked" below); the application database was never
  touched.
* **Error contract.** `403 FORBIDDEN` for a non-Admin actor reaching the service
  (defence in depth behind the route), `400 LAST_ADMIN_PROTECTED`,
  `SELF_ROLE_CHANGE`, `SELF_DEACTIVATION`, `USER_ALREADY_EXISTS`,
  `USER_NOT_FOUND`, `INVALID_ROLE`, `PASSWORD_TOO_SHORT`,
  `ADMIN_ALREADY_EXISTS` (provisioning) and
  `503 SESSION_REVOCATION_FAILED`; all messages are fixed, and no SQL, path,
  hash, token or driver text is echoed.

**Tests**

* `tests/test_user_service.py` — service-level behaviour on fakes, including the
  regressions "a non-Admin can be deactivated when only one active Admin
  exists", "the last active Admin cannot be deactivated", "the last active
  Admin cannot be demoted" and "deactivating one of two Admins leaves one".
* `tests/test_api_user.py` — 28 tests: authentication, `users:manage` (403 for
  non-Admins), creation, duplicate/invalid input, mass-assignment rejection,
  role change, deactivation, self-change refusal, no credential leakage,
  session revocation refusing further use of the deactivated account's token
  while other sessions stay valid, the sanitized `503
  SESSION_REVOCATION_FAILED` envelope, plus tests that build the **real**
  `create_app` and assert the four routes are registered, require authentication
  on all four, enforce `users:manage`, and work for an Admin (list, create,
  role change, deactivation).
* `tests/test_user_provision.py` — 7 offline CLI tests: importability/`--help`,
  successful provisioning (identity-only output, no password or hash),
  refusal when an active Admin exists, duplicate identity translated to a safe
  message, audit-unavailable abort with no user created, sanitized
  invalid-input failure, and no provisioning route in `APPROVED_ROUTES`.
* `tests/test_tier2_user_concurrency.py` — 7 real-PostgreSQL scenarios using
  independent connections, a `threading.Barrier`, and final committed-state
  assertions: two simultaneous first-Admin provisions, two Admins deactivating
  each other, two Admins demoting each other, concurrent Admin creation plus
  demotion, safe non-Admin deactivation and safe non-Admin role change with one
  active Admin, and the enforced `READ COMMITTED` proven against a
  `REPEATABLE READ` session default. The suite refuses to touch foreign Admin
  rows, through a separate test-only `attendance_tier2_cleanup`
  credential (the runtime role has no `DELETE`). That credential holds
  `DELETE` on the two security tables plus column-level `SELECT` on `user_id`
  only, and it is verified as *effective* privilege (including anything
  inherited through role membership) together with the database name, the
  cluster `system_identifier` and the absence of mutual membership. **BLOCKED**:
  see below.
* `tests/test_apply_v002_owner_script.py` — 15 static review tests of the
  owner script (no database): embedded V002 byte-identical to the migration
  file, ledger checksums equal to the loader's, one transaction around
  DDL + ledger + grants, exactly the least-privilege grant set, the
  effective-privilege assertions (`has_table_privilege`/
  `has_column_privilege` over every catalog column) for both roles, and a
  regression test that a role name (`grantee`) is never resolved through
  `pg_get_userbyid()`.
* `tests/test_cleanup_credential_verification.py` — 19 offline regression tests
  that drive the real `_require_cleanup_credential()` against a synthetic
  psycopg-shaped connection: the minimum permission set is accepted and each
  expansion is rejected (missing `DELETE`, missing `user_id` read, table-level
  `SELECT`, a read on another column, a `SELECT` inherited through another
  role, column- and table-level writes, an over-privileged runtime role), and
  every identity failure is rejected (wrong database, wrong cluster,
  unreadable or unexecutable `pg_control_system()`, wrong role, superuser,
  mutual membership).

**Blocked / not verified**

* The dedicated PostgreSQL test database (`visual_attendance_tier2_test`) is
  reachable and passes the tier2 guard, but its schema is still V001
  (`schema_version` = 1; tables `employee`, `schema_version`,
  `_phase3_test_guard`). **Verified cause:** the configured
  `ATTENDANCE_TEST_DATABASE_URL` authenticates as `attendance_tier2_runtime`,
  which has no `CREATE` on schema `public` and does not own the existing
  tables, so neither the application nor this suite can apply
  `migrations/V002__security.sql` there. A real attempt through
  `src.db_migrate.migrate` with `PsycopgMigrationExecutor` was refused by the
  server (`InsufficientPrivilege: permission denied for schema public`), rolled
  back entirely, and left the database unchanged (still version 1).
* Therefore the seven scenarios in `tests/test_tier2_user_concurrency.py` skip
  with an explicit `BLOCKED` reason instead of failing with `UndefinedTable`,
  and the race-safety claim rests on the advisory-lock design plus fake-based
  tests only. They are reported **BLOCKED**, not passed; no fake-only test is
  presented as concurrency verification.
* **Owner runbook** — one guarded, single-transaction script
  (`tools/apply_v002_tier2_test_database.sql`), because
  `tools/run_migrations.py` deliberately refuses the dedicated test target and
  the configured DSN (`attendance_tier2_runtime`) has no `CREATE` on schema
  `public`:

  1. Owner-side role setup (once; the script never creates roles).
     `attendance_tier2_runtime` already exists. Create the **separate,
     test-only** teardown role as an ordinary non-superuser LOGIN role:
     `CREATE ROLE attendance_tier2_cleanup LOGIN PASSWORD '<owner-chosen>';`
     and put its DSN in the local, untracked environment as
     `ATTENDANCE_TIER2_CLEANUP_DATABASE_URL`. It is used only by the tier-2
     suite's teardown, never by application code.
  2. Confirm the migration file the script reproduces is unmodified:
     `sha256sum migrations/V002__security.sql` must print
     `35bc00cd7a947a9eca31a7502d3a17e0ff55163f1c5222cf8fcb88a6328082d3`
     (the runner's canonical checksum, so line endings do not matter).
  3. Run the script as a role with `CREATE` on schema `public`, from the
     repository root (grant option on that schema is needed only if
     `attendance_tier2_cleanup` does not already hold `USAGE` there; see
     step 6):
     `psql -v ON_ERROR_STOP=1 --dbname "<owner DSN for visual_attendance_tier2_test>" --file tools/apply_v002_tier2_test_database.sql`
     Any SQL client works (the script contains no psql meta-commands) but it
     must be executed as **one** script, never statement by statement.
     Preconditions: exact database name; cluster identity; no pre-existing
     V002 object name; ledger exactly `[1]` with the V001 checksum; unchanged
     guard marker; both roles present, non-superuser and not members of each
     other; and the executing role's own `CREATE` authority.
  4. Why one transaction is mandatory: `src.db_migrate`'s contract is that the
     DDL and its `schema_version` row commit together, with `checksum` =
     SHA-256 of the file's canonical text and a timezone-aware `applied_at`.
     Applying the DDL and inserting the ledger row as two steps could leave a
     committed `app_user` with no ledger row — a state the runner refuses to
     repair — if the second step failed. (The first draft runbook described
     two steps; this script replaces it.)
  5. The verification block refuses to commit unless all of this holds: both
     tables; all 8 + 7 columns with exact types, nullability and defaults
     (`session_token_hash` is `character(64)`, `is_active` defaults to `true`,
     timestamps have no default); both primary keys; the `username_key` UNIQUE
     constraint; the single `role` CHECK naming all four roles; the
     `user_session.user_id` foreign key to `app_user(user_id)`; both indexes;
     ledger rows `[1, 2]` with both checksums; the unchanged four-column
     `employee` table and its primary key; the unchanged guard marker.
  6. Privileges (least privilege, all in the same transaction):
     `SELECT, INSERT, UPDATE` on `app_user` and `user_session` for
     `attendance_tier2_runtime` — **no `DELETE`** (the application never
     deletes; deactivation is an `UPDATE`) and **no column-level grant at
     all**; and, for `attendance_tier2_cleanup`, **`DELETE` on those two tables
     only** plus **column-level `SELECT` on `user_id` only** (the column its
     `DELETE ... WHERE user_id = ANY(%s)` statements need: no table-level read,
     no `INSERT`/`UPDATE`, no other column), plus `USAGE` on schema `public`
     **only when it is not already held** (PostgreSQL grants `USAGE` on
     `public` to `PUBLIC` by default, and a `GRANT` needs grant option even
     when the privilege exists, so the script checks first and demands grant
     option only when a grant is actually required). No ownership, no
     `TRUNCATE`, no `ALTER`, no DDL privilege for either role.
     The script
     asserts that the tables' owner is the executing role before granting, and
     the verification block fails if either role holds anything beyond its set.
  7. Effective-privilege verification (blocks 5n, 5n-bis, 5o, 5p). The
     verification does not trust the grant records the script wrote: it asks
     PostgreSQL, so a privilege inherited through a role membership is
     included. `has_table_privilege()` is used for `DELETE`/`INSERT`/`UPDATE`;
     `has_column_privilege()` is used for `SELECT` on **every** column of both
     tables. That function succeeds when the privilege is held for the whole
     table *or* granted for that column, so one per-column sweep rejects a
     table-level `SELECT` grant, a grant on any other column and an inherited
     read, and its column list comes from `information_schema.columns` (a later
     migration's columns are covered without editing the script). The same
     sweep rejects column-level writes for the cleanup role and any
     column-level grant for the runtime role. Role names are compared **as
     names**: `grantee` in `information_schema.column_privileges` is already a
     role name, so it is never passed to `pg_get_userbyid()`, which resolves an
     OID — doing so is a type error that fails the apply, and
     `tests/test_apply_v002_owner_script.py` pins the regression.
  8. Server identity: the precondition requires
     `pg_control_system().system_identifier` to equal
     `7687968999847354228`, the value captured read-only from the guarded
     tier-2 target on 2026-11-19 (a cluster fingerprint, not a credential).
     Limitation: this pins the *cluster*, not the network path — the owner must
     still confirm the endpoint independently (DSN dbname/host/port, or the
     client's connection-info command) before running.
     `inet_server_addr()`/`inet_server_port()` are reported but not enforced,
     because a legitimate owner session may arrive over another interface or a
     local socket. If the disposable cluster is deliberately re-initialised,
     capture the new identifier read-only and update the constant in the same
     reviewed change.

     `pg_control_system()` needs `EXECUTE` on the function, which PostgreSQL
     grants to `PUBLIC` by default: no superuser role and no `pg_monitor`
     membership is required. Evidence for this cluster: the pinned value was
     captured through the **non-superuser** `attendance_tier2_runtime` session
     (read-only verification below), and the suite's cleanup check reads the
     same function through the cleanup credential. If a hardened cluster has
     revoked that `EXECUTE`, both paths fail closed with a named blocker
     (`IDENTITY_UNVERIFIABLE` in the guard; an explicit "cannot read
     pg_control_system()" failure in the suite) instead of skipping the cluster
     check: the guard never falls back to comparing database names alone.
  9. Rerun `ATTENDANCE_TIER2_OPT_IN=1 .venv/Scripts/python.exe -m pytest
     tests/test_tier2_user_concurrency.py -q` and record the result here.

  Verified read-only on the guarded target before writing the script
  (2026-11-19): the canonical checksum of `migrations/V002__security.sql` is
  `35bc00cd7a947a9eca31a7502d3a17e0ff55163f1c5222cf8fcb88a6328082d3`, matching
  the value the loader derives, `tests/test_migration_v002.py` and
  `MIGRATION_SET_DIGEST`; the ledger holds only `(1, 9e6ac6b5…deac9)`; no
  `app_user`, `user_session` or V002 index name exists in any schema; and the
  session reports `current_database() = visual_attendance_tier2_test`,
  `current_user = attendance_tier2_runtime`, `rolsuper = false`,
  `has_schema_privilege('public','CREATE') = false`, database owner
  `postgres`, `system_identifier = 7687968999847354228`, server 18.6. The
  script's precondition and verification blocks were executed read-only
  (rolled back) and behaved as intended, and
  `tests/test_apply_v002_owner_script.py` checks the embedded V002 block
  byte-for-byte against the migration file. No privileged connection to this
  target was available to the agent, so the apply itself has **not** been
  executed.

  Until step 8 passes, P5.6's database concurrency verification remains
  unproven.
* No live server, real credentials, frontend, P5.7/P5.8/P5.9 work or production
  database was exercised.

### 10.2 P5.7–P5.9

**P5.7 — Frontend authentication and session integration — implemented,
awaiting review** (authorized by the owner on 2026-11-20 after accepting P5.6
with its PostgreSQL concurrency verification recorded as BLOCKED). Delivered:
the `/login` page, a single auth state model (`loading` / `authenticated` /
`unauthenticated` / `error`) with one `/me` request per page load, session
restoration after refresh, a client-side guard on the `(dashboard)` route group,
safe return-path validation, sign-out from the shell header, and sanitized
failure text. The guard is client-side because the session cookie is
`Path=/api/v1`, so the browser never sends it to a dashboard route — middleware
or a server component could not see it, and neither the cookie path nor a
second cookie was widened. Offline verification: 541 frontend tests pass across
38 files (39 of them new, in 5 new test files), `eslint .` clean,
`tsc --noEmit` clean, `next build` succeeds. No live backend, browser session or production deployment was
exercised. Details and limitations: `docs/P5_7_FRONTEND_AUTHENTICATION.md`.

**P5.8 — Role-aware UI and frontend security hardening — implemented, awaiting
review.** Delivered: one presentation-layer mirror of the P5.4.10 matrix
(`src/lib/authz/permissions.ts`) with fail-closed lookups for unknown roles and
permissions, a single `useAuthorization()` entry point, permission-filtered
navigation (desktop and drawer from the same table), `RequirePermission` page
guards with one consistent access-denied screen, hidden (not disabled)
create/rename/deactivate/check-out controls per role with a second check in
each handler, and a client error policy that separates 401 (clears the session
UI and redirects once), 403 (session kept, denied message, no retry) and
503/network (unavailable, explicit retry). Verified offline: 614 frontend tests
across 42 files pass (71 new, in 4 new files), `eslint .` clean,
`tsc --noEmit` clean, `next build` succeeds. **Not** delivered, deliberately:
no user-administration UI (the `/api/v1/users` routes are absent from the proxy
allowlist, which this milestone froze, and P5.6's PostgreSQL concurrency
verification is still BLOCKED) and no audit view (`audit:read` guards no
endpoint, so a page would be inventing a capability). No live backend, browser
or deployment was exercised. Details: `docs/P5_8_ROLE_AWARE_UI_SECURITY.md`.

**P5.9 — Final security regression and Phase 5 acceptance — review complete,
awaiting owner acceptance (2026-11-20).** Delivered: an integrated review of
authentication, backend RBAC, the proxy allowlist, the audit journal and
rotation, user administration, the admin invariant and database guards, and the
P5.7/P5.8 frontend layers; the full backend suite (**4006 passed, 40 skipped**),
the full frontend suite (**614 passed / 42 files**), `compileall`, `eslint`,
`tsc --noEmit`, `next build` (routes unchanged) and `git diff --check`, all
exit 0, plus the 15-file audit suite (**783 passed, 1 skipped**) after the
exclusion-sweep count was reconciled from a recorded 50 to the actual 52 tests;
and an 18-section acceptance record
(`docs/P5_9_PHASE5_FINAL_ACCEPTANCE.md`), including a documentation
reconciliation, a file-change inventory and a Git-restrictions compliance
section.

One **source defect** was found and fixed (finding F1): the four
user-administration mutations built their audit context with a fresh
`uuid4()` instead of the request's server-generated ID, so `user.create`,
`user.role_change`, `user.deactivate` and `first_admin.provision` records could
not be correlated with the response's `X-Request-ID` or its logs. `UserService`
now takes an optional `request_id`, resolves it through the current request
context and never fabricates one; `src/api_user.py` passes
`request_id_for(request)`; `tests/test_user_audit_request_id.py` (4 tests) pins
the behaviour. Findings F2 (user actions reuse `EMPLOYEE_*` reason codes) and F3
(already-inactive deactivation returns success unaudited) are disclosed rather
than changed, because both would alter the approved audit event matrix.

**P5.9 did not:** add endpoints, expand the proxy allowlist, execute migrations
or SQL, create roles, connect to a database, stage/commit Git, or claim live
browser/camera/deployment verification. Real-PostgreSQL concurrency
verification for P5.6 remains **BLOCKED** and is recorded as such. Phase 5 is
**not** accepted; the acceptance decision is the owner's.
