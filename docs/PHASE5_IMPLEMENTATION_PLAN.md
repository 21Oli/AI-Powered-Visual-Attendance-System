# Phase 5 — Authentication, Accounts and Role-Based Access

**Status: in progress — P5.3 implemented offline (no live PostgreSQL run).**

Phase 5 adds operator accounts, sessions and role-based access to the
attendance system. It is delivered as small, separately reviewable steps. This
document records the phase plan, the decisions that shape it, and what is
actually implemented versus deferred.

## 1. Scope

Phase 5 covers:

* the identity schema (users and sessions) and its persistence layer;
* password hashing and credential verification;
* session issuance, validation, refresh and revocation;
* login/logout endpoints and an authentication dependency;
* role-based authorization for the existing attendance and employee APIs;
* the frontend sign-in flow.

It does **not** change employee or attendance-journal semantics, does not add an
audit table, and does not introduce biometric storage of any kind.

## 2. Milestones

| Milestone | Scope | Status |
| --- | --- | --- |
| P5.1 | Phase 5 inspection, gap analysis and approved decisions | Completed (review, no code) |
| P5.2 | Identity data model and persistence: `V002__security.sql`, user/session models and repositories, offline tests | Completed (offline) |
| **P5.3** | **Authentication primitives: Argon2id hashing, opaque session tokens, session lifecycle** | **Completed (offline)** |
| P5.4 | Session issuance, validation and revocation service | Not started |
| P5.5 | Login/logout endpoints and the authentication dependency | Not started |
| P5.6 | Role-based authorization for existing endpoints | Not started |
| P5.7 | Frontend sign-in, session handling and Phase 5 acceptance | Not started |

Each milestone is gated on owner review of the previous one. No milestone
starts early, and no milestone may require a live database or secrets without a
separately authorized run.

## 3. Approved decisions carried into Phase 5

* **Roles**: exactly `Admin`, `HR Manager`, `Operator`, `Auditor`, enforced by a
  database `CHECK` and mirrored by a Python enum. No role hierarchy or
  inheritance; permissions are decided per endpoint in P5.6.
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

P5.4 (session issuance/validation service integration — cookie and request
plumbing), gated on owner approval of P5.3.

## 8. P5.4 authentication API — sub-milestone status

P5.4 is delivered in separately reviewed sub-milestones. Nothing beyond the
completed rows exists in the tree.

| Sub-milestone | Scope | Status |
| --- | --- | --- |
| P5.4.1 | Read-only inspection of the P5.3 primitives, backend, proxy and tests | Completed (review, no code) |
| P5.4.2 | Authentication configuration (session cookie, environment Secure rule, login throttle bounds) | Completed (offline) |
| P5.4.3 | Request ID and correlation infrastructure (server-generated UUID, `X-Request-ID`, context access, log correlation) | Completed (offline) |
| P5.4.4+ | Cookie utilities, authentication dependency, `/auth/login`, `/auth/logout`, `/auth/me`, route protection, login throttling (P5.4.9), integration tests and security review | Not started |

P5.4.2 adds configuration only (`src/config.py`, `SESSION_COOKIE`,
`SECURE_COOKIE_ENVIRONMENTS`, `LOGIN_THROTTLE`, `AppSettings.session_cookie_secure`).
The approved values are `va_session` on `/api/v1`, `HttpOnly`, `SameSite=Lax`,
no `Domain`, `Max-Age` 43,200 s, `Secure` in staging/production only, 8-hour
idle timeout, 12-hour absolute lifetime, and a 5-attempt / 15-minute login
throttle keyed by normalized username with no permanent lockout. No switch
exists to trust client request IDs, trust `X-Forwarded-For`, disable CSRF
validation in staging/production, or lock an account permanently.
