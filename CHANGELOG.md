# CHANGELOG.md

All notable changes to the AI-Powered Visual Attendance System.
Contains **actual implemented changes only** — future work is not listed
as done. Format based on Keep a Changelog.

## [Unreleased] — Phase 5, P5.3: authentication primitives

### Added

* `argon2-cffi==25.1.0` — the one approved authentication dependency (Argon2id, Python 3.14 / Windows x64 wheels verified). Transitive `argon2-cffi-bindings` and `cffi` are resolved by pip and are not pinned separately.
* `src/config.py` — `PasswordHashingConfig` / `PASSWORD_HASHING` (Argon2id, memory 19,456 KiB, time 2, parallelism 1, salt 16 B, hash 32 B, minimum password 8 characters, defensive maximum 1,024) and `SessionPolicyConfig` / `SESSION_POLICY` (8-hour idle timeout, 12-hour absolute lifetime, 256-bit token entropy), both frozen and validated.
* `src/password.py` — `PasswordService.hash_password` / `verify_password` / `needs_rehash` with `PasswordPolicyError` and `InvalidPasswordHash`. Passwords are never trimmed or normalised; verification returns `False` only for a genuine mismatch and fails closed on malformed, non-Argon2id, wrong-version or over-budget stored hashes; the returned encoding is re-validated.
* `src/session_token.py` — `generate_session_token` (256 bits from `secrets`, 43 base64url characters), `session_token_digest` (lowercase SHA-256 hex for `CHAR(64)`), `is_valid_session_token` (strict 43-character/32-byte structural check) and `SessionTokenError`.
* `src/session_service.py` — `SessionService.issue_session` / `validate_session` / `revoke_session` / `revoke_all_for_user` with `IssuedSession`, `AuthenticatedSession` and typed `AuthenticationError` subtypes (`InvalidSessionToken`, `SessionExpired`, `SessionRevoked`, `UserInactive`, `UserUnknown`), plus `SessionIssueError` and `ClockError`. Activity slides the idle deadline but is clamped to `absolute_expires_at`; inactive and missing accounts are refused.
* `src/clock.py` — `utc_now` and the `Clock` callable seam so tests inject deterministic time.
* Offline suites `tests/test_password.py`, `tests/test_session_token.py`, `tests/test_session_service.py` — 90 tests using the real hasher and in-memory repository doubles with a mutable injected clock.

### Changed

* `requirements.txt` — adds the pinned `argon2-cffi==25.1.0` with its rationale; no other pin changed.
* `docs/PHASE5_IMPLEMENTATION_PLAN.md` — P5.3 marked completed with its delivered scope, decisions and verification status; next step is P5.4.
* `DEVELOPMENT_STATUS.md` — Step 52 records the P5.3 implementation.

### Verified

* Focused suites: **90 passed** (`tests/test_password.py`, `tests/test_session_token.py`, `tests/test_session_service.py`).
* Full offline suite: **2540 passed, 32 skipped**, exit 0. No live database, `.env`, secret, camera or biometric data was accessed.

### Notes

* Primitives only: there is no login/logout/me endpoint, no cookie, CSRF, throttling, RBAC, audit logging, user administration or frontend authentication, and no migration was executed.
* `validate_session` slides `expires_at` and records `last_seen_at`; it does not revoke expired rows (a sweep is later work).
* Argon2id cost was not benchmarked on target hardware; the approved parameters are OWASP's 19 MiB profile, not a measured latency budget.

## [Unreleased] — Phase 5, P5.2: identity data model and persistence

### Added

* `migrations/V002__security.sql` — `app_user` (identity, role `CHECK` in `Admin`/`HR Manager`/`Operator`/`Auditor`, active flag, application-authored timestamps) and `user_session` (`CHAR(64)` hashed-token primary key, `user_id` foreign key, sliding and absolute expiry, `revoked_at`, `last_seen_at`), with justified indexes on `user_session.user_id` and `user_session.expires_at` and no database timestamp defaults.
* `src/user.py` — `Role`, `User`, `normalize_username`, `username_key_for`, `UserValidationError` (NFKC normalisation, whitespace collapsing, case-insensitive key, 3–64 characters, control characters rejected, `password_hash` hidden from `repr`).
* `src/user_session.py` — `UserSession`, `is_valid_session_token_hash`, `SessionValidationError`.
* `src/user_repository.py`, `src/session_repository.py` — parameterised create/find/list/update and insert/find/touch/revoke/revoke-all operations with explicit error types; no hashing, issuance, auth dependency or endpoint code.
* `src/db_security_repository.py` — `open_user_repository` / `open_session_repository` with one connection per operation, commit on success and rollback on failure.
* `src/config.py` — `UserPaginationConfig` and `USER_PAGINATION` (default 50, maximum 100) so account listing is bounded.
* `docs/PHASE5_IMPLEMENTATION_PLAN.md` — Phase 5 milestones, approved decisions, P5.2 scope and open limitations.
* Offline test suites: `tests/test_user.py`, `tests/test_user_session.py`, `tests/test_user_repository.py`, `tests/test_session_repository.py`, `tests/test_db_security_repository.py`, `tests/test_migration_v002.py`, plus the shared fake in `tests/security_support.py`.

### Changed

* `src/schema_contract.py` — declares `app_user` and `user_session`, `CONTRACT_MIGRATION_VERSION = 2`, four required tables and the new migration-set digest (V001 is unchanged).
* `tests/test_schema_contract.py`, `tests/test_physical_schema.py`, `tests/test_api_physical_gate.py`, `tests/test_startup_schema.py`, `tests/test_db_migrate_executor.py`, `tests/test_db_migrate_executor_catalog.py`, `tests/test_run_migrations_cli.py`, `tests/test_negative_schema_scenarios.py`, `tests/test_provision_negative_schema_cases.py` — moved from a one-migration to a two-migration world (four contracted tables, pending `2`, applied `[1, 2]`).
* `tools/negative_schema_scenarios.py`, `tools/provision_negative_schema_cases.py` — security tables merged into the declared state, two-version applied history and shipped-checksum mappings.
* `DB_DESIGN.md` — new §12 documents the V002 identity schema and its constraints.

### Verified

* Full offline suite: **2450 passed, 32 skipped**, exit 0 (`.venv/Scripts/python.exe -m pytest tests/ -q`). No live database, `.env`, secret or dependency was used.

### Notes

* P5.2 stops at the data model. There is no password hashing, session issuance, login endpoint, authentication dependency or RBAC; P5.3 was not started.
* `V002__security.sql` has not been applied to PostgreSQL and the repositories have not been exercised against a live database — both remain unverified until a separately authorized run.
* The schema contract can express tables, columns and primary keys only; unique constraints, the foreign key, the role `CHECK` and indexes are asserted against the migration text in `tests/test_migration_v002.py`.

## [Unreleased] — Phase 4, P4.9: final regression and acceptance

### Verified

* `npx vitest run` exit 0 — **457 passed / 457**, 31 suites (49.66 s); `npm run lint` exit 0 — 0 problems; `npx tsc --noEmit` exit 0; `npm run build` exit 0 (static `/`, `/_not-found`, `/employees`; dynamic `/attendance`, `/overview`, `/system-status`, `/api/backend/[...path]`); `npm ls --depth=0` exit 0 — the same 15 pinned packages. All run in this milestone on `4629412` with a clean working tree.

### Changed

* `docs/PHASE4_IMPLEMENTATION_PLAN.md` — P4.9 milestone row marked completed, a new §10 records the final regression evidence and acceptance decision, and the earlier phase-status wording now points at it.
* `docs/PHASE4_DASHBOARD_PROPOSAL.md`, `frontend/README.md`, `DEVELOPMENT_STATUS.md` — record the acceptance decision, the verified results and the remaining deferred work.

### Notes

* **Phase 4 decision: `PHASE 4 ACCEPTED` for the documented dashboard scope** — four functional screens, exactly the eight approved API pairs, unchanged proxy controls, development-only fixtures, append-order attendance with the shared session fold, and a bodyless, single-flight, success-only-after-201 operator check-out.
* This is not a production-readiness claim. No authentication/RBAC, a loopback-only backend, no multi-process JSONL locking, no residual-session reconciliation, client-side attendance filtering/paging and manual `PERSISTENCE_UNCONFIRMED` verification remain, and live PostgreSQL/migrations, real-camera/biometric accuracy, real-browser and deployment validation were not performed.
* The review changed documentation only; no application code, test, dependency, backend, database or Git state was touched.

## [Unreleased] — Phase 4, P4.8: documentation-only closure

### Changed

* `docs/PHASE4_IMPLEMENTATION_PLAN.md` — the P4.8 milestone row now records completion by prior implementation, and a new §9 documents the closure: the eight approved routes, the four write workflows and the proxy security controls were already delivered in P4.4–P4.7.
* `docs/PHASE4_DASHBOARD_PROPOSAL.md`, `frontend/README.md`, `DEVELOPMENT_STATUS.md` — status and roadmap wording now record P4.8 as closed without new code.

### Notes

* **P4.8 status: COMPLETED — REQUIREMENTS SATISFIED BY P4.5–P4.7.** No new P4.8 implementation, test, dependency, proxy or API-contract change was required or made; the milestone changed documentation only.
* Phase 4 remains incomplete: P4.9 (final verification, documentation and acceptance review) is not started. All previously recorded limitations stand, including no authentication/RBAC, a loopback-only backend, manual `PERSISTENCE_UNCONFIRMED` verification, and no live PostgreSQL, real-camera/biometric, real-browser or deployment validation.

## [Unreleased] — Phase 4, P4.7: System Status screen

### Added

* `frontend/src/lib/api/health.ts` — typed `fetchHealth` over the existing `requestJson`/`API_PATHS.health`/`parseHealth`, accepting only `{ "status": "ok" }`, with a documented 8-second `AbortSignal.timeout` combined with the caller's signal and a distinct `HEALTH_TIMEOUT` failure that never mislabels an unmount as a timeout.
* `frontend/src/components/system-status/SystemStatusView.tsx` + CSS Module — loading, healthy (only after a validated response), unreachable, timeout, unexpected-response and server-error states, UTC last-attempt/last-success times, one guarded manual refresh, fixture-mode disclosure without a fabricated status, and an explicit scope panel naming what is not verified.

### Changed

* `frontend/src/app/(dashboard)/system-status/page.tsx`: the placeholder became the functional dynamic health screen, with fixture mode resolved on the server.
* `frontend/tests/routes.test.tsx`: System Status is no longer a placeholder; the route test now expects the health screen and the fetch mock answers `/health`.
* `frontend/README.md`, `docs/PHASE4_IMPLEMENTATION_PLAN.md`, `docs/PHASE4_DASHBOARD_PROPOSAL.md`, `DEVELOPMENT_STATUS.md`: document the health contract, the screen's states and the limits of a liveness check.

### Verified

* `npx vitest run` exit 0 — **457 passed / 457**, 31 suites; `npm run lint` exit 0; `npx tsc --noEmit` exit 0; `npm run build` exit 0 (dynamic `/system-status`); `npm ls --depth=0` exit 0 — the same 15 pinned packages.

### Notes

* The health endpoint is a liveness probe: it does not check PostgreSQL, schema or migrations, cameras, recognition, enrolment, authentication or deployment, and the page states that rather than inventing indicators. `GET /api/v1/employees` remains the honest database probe.
* No polling, automatic retry, reconciliation, backend change, proxy change or dependency was added; fixture mode never fabricates a healthy backend result.
* Live backend, real-browser and deployment validation remain unperformed.

## [Unreleased] — Phase 4, P4.6.6: final review and documentation corrections

### Changed

* `frontend/src/lib/fixtures/overview-fixtures.ts`, `frontend/next.config.ts` — corrected two stale doc comments only (the fixture journal is 25 events and FIX-003 is closed; the proxy arrived in P4.4, not P4.8). No code, contract or behaviour change.
* `frontend/README.md`, `docs/PHASE4_IMPLEMENTATION_PLAN.md`, `docs/PHASE4_DASHBOARD_PROPOSAL.md` — corrected statements that still described the dashboard as checkout-free or fixture mode as Overview-only, refreshed the milestone statuses and test counts, and added the P4.6.6 review section with its measured results.

### Verified

* `npx vitest run` exit 0 — **439 passed / 439**, 29 suites; `npm run lint` exit 0; `npx tsc --noEmit` exit 0; `npm run build` exit 0 (static `/`, `/_not-found`, `/employees`, `/system-status`; dynamic `/attendance`, `/overview`, `/api/backend/[...path]`); `npm ls --depth=0` exit 0 — the same 15 pinned packages.

### Notes

* Review-only milestone: no feature, proxy rule, API contract, backend/database file or dependency changed.
* Phase 4 decision: `PHASE 4 INCOMPLETE — BLOCKERS REMAIN`. P4.1–P4.6.5 are implemented and pass automated verification, but the System Status screen (P4.7) is still a placeholder and P4.8–P4.9 are not started. Live backend, real-browser and deployment validation remain unperformed.

## [Unreleased] — Phase 4, P4.6.5: operator check-out workflow

### Added

* `frontend/src/lib/attendance/checkout.ts` — the eligibility matrix (fixture mode, directory loading/unavailable, unresolved id, proxy-unsupported id, inactive employee, indeterminate or closed session, request already pending) with one explanation per refusal, plus operator-facing copy for every classified check-out failure and the informational cooldown default.
* `frontend/src/components/attendance/AttendanceCheckout.tsx` + CSS Module — one panel listing employees with journal events, their derived session state, a `Check out` action only when every rule holds, explicit reasons otherwise, a `role="status"` pending line, success/error messages and an explicit `Refresh journal` step for uncertain outcomes.

### Changed

* `frontend/src/components/attendance/AttendanceView.tsx`: renders the check-out panel for the loaded journal and passes the derived session states and a reload callback; the journal itself is still never edited and no optimistic event is inserted.
* `frontend/tests/attendance-page.test.tsx`: the unknown-employee assertions are now scoped to the journal table, because the check-out panel also renders names and ids.
* `frontend/README.md`, `docs/PHASE4_IMPLEMENTATION_PLAN.md`, `docs/PHASE4_DASHBOARD_PROPOSAL.md`, `DEVELOPMENT_STATUS.md`: document the operator check-out and its safety behaviour.

### Notes

* A check-out is confirmed in the shared dialog and sent exactly once through the existing `checkoutEmployee` client: no body, no `Content-Type`, no retry, no abort signal (cancelling a write mid-flight would invent exactly the uncertainty the UI must not create).
* Success is shown only after the service returns a validated check-out event and the journal reloads; the session closes only because the refreshed journal says so.
* `503 PERSISTENCE_UNCONFIRMED` is presented as uncertain: the event may or may not be recorded, it is never retried or described as success or definite failure, and the operator is asked to refresh the journal and verify the latest event first.
* Fixture mode offers no check-out action at all and never mutates a fixture record; no backend, proxy, database or dependency change was made.

## [Unreleased] — Phase 4, P4.6.4: read-only Attendance journal

### Added

* `frontend/src/lib/attendance/format.ts` — `YYYY-MM-DD HH:MM:SS UTC` timestamps, `Unavailable` for an unreadable one (never `Invalid Date`), explicit `Unknown type (…)` labels, session-state labels and UTC-day option labels.
* `frontend/src/lib/attendance/summary.ts` — journal counts (checks in/out, undated, unclassified, employees with events, open/closed sessions) and the distinct UTC days present, both computed from the shared fold.
* `frontend/src/components/attendance/` — `AttendanceView` (concurrent journal + full-directory load, abortable and request-tagged, fixture banner, torn-journal warning, two independent error panels with Retry, empty/no-match states), `AttendanceTable` (append order, resolved names with ids, UTC timestamps, current session state, stacked cards below 720 px), `AttendanceFilters` (four labelled selects, live count, reset) and `AttendancePagination` (client-side, 10/20/50/100), each with a CSS Module.
* `frontend/src/app/(dashboard)/attendance/page.tsx`: the placeholder became the read-only journal (`metadata.title = "Attendance"`, `dynamic = "force-dynamic"`).
* Tests: `attendance-page.test.tsx` (25), `attendance-table.test.tsx` (9) and `attendance-display.test.ts` (12) — 46 new tests.
* Earlier P4.6 commits (`0a8fd55`, `c0e5e77`) had already added `attendance/journal.ts`, `attendance/filters.ts`, `attendance/pagination.ts`, `classifyCheckoutError` and `checkoutEmployee`; this entry records the UI built on the first three.

### Changed

* `frontend/src/lib/fixtures/overview-fixtures.ts`: the attendance fixture grew to 25 deterministic events over three UTC days for `FIX-001`…`FIX-006`, covering both event types, open and closed sessions, an inactive employee with a historical open session, one undated record, and a record at the fixture clock instant so two clocks never produce identical data. `fixtureEmployees`, `FIXTURE_LABEL` and `isFixtureModeEnabled` are unchanged.
* `frontend/tests/routes.test.tsx`: Attendance is no longer asserted as a placeholder; the route test now expects the functional journal.
* `frontend/README.md`, `docs/PHASE4_IMPLEMENTATION_PLAN.md`, `docs/PHASE4_DASHBOARD_PROPOSAL.md`, `DEVELOPMENT_STATUS.md`: document the read-only journal, its client-side filters and paging, the fixture behaviour and the remaining limitations.

### Notes

* The page is read-only: it renders no checkout control, imports no mutation and issues no POST/PATCH/PUT/DELETE. `checkoutEmployee` and its error classifier remain unused by the UI.
* The whole journal is downloaded once and filtered/paged in the browser because the API has no attendance query parameters; a large journal will eventually need a backend-side limit.
* The session filter has no `unknown` option (the shared filter contract has none), so stateless events appear only under `All statuses` while the table labels them `Unknown`.
* No live backend or browser verification: the FastAPI startup gate needs PostgreSQL, so all evidence is from mocked responses and development fixtures.

## [Unreleased] — Phase 4, P4.5: Employees management

### Added

* `frontend/src/lib/employees/validation.ts` — employee form rules. The identifier rule **delegates to the proxy's own `isSafeEmployeeId`** instead of copying it, so the form accepts exactly what the proxy can address and the two cannot drift. Ids are never trimmed, case-folded or otherwise normalised, and values such as `0007`, `emp-0001` and `EMP_1.2` remain valid.
* `frontend/src/lib/employees/format.ts` — UTC `YYYY-MM-DD` creation dates, the explicit `Not recorded` placeholder for a nullable `created_at`, `Unavailable` for an unreadable timestamp (never `Invalid Date`), and status labels.
* `frontend/src/lib/employees/errors.ts` — API failure → form-field mapping: `EMPLOYEE_EXISTS` to the id field, `body.`-prefixed pydantic locations stripped before matching, unmappable field errors preserved as a general message, and proxy refusals (`PROXY_REQUEST_INVALID`, 413) never presented as backend field validation.
* `frontend/src/lib/api/employees.ts` — `createEmployee` (POST, three contracted fields, `is_active: true`, no `created_at`), `fetchEmployeeById`, `renameEmployee` (PATCH with `full_name` only) and `deactivateEmployee` (POST, no body), all through `requestJson`, `API_PATHS` and the existing `ApiError`.
* `frontend/src/components/ui/{TextField,ConfirmDialog,StatusMessage}` — the primitives P4.5 actually needed: a labelled input with hint/`aria-describedby`/`aria-invalid` error, a `role="dialog"` confirmation with focus to Cancel, Tab trap, Escape and focus restoration, and a status/alert region that only reports success after the API resolves.
* `frontend/src/components/employees/` — `EmployeesView` (keyset paging with a cursor history, abortable and request-tagged fetches), `EmployeeTable` (a semantic table restyled into stacked cards below 720 px), `PaginationControls`, `CreateEmployeeForm` and `EmployeeDetail` (detail read plus rename and a deactivate hand-off), each with a CSS Module.
* Tests: `employee-id.test.ts`, `employees-errors.test.ts`, `employees-api.test.ts`, `employees-directory.test.tsx`, `employees-create.test.tsx`, `employees-rename.test.tsx` and `employees-deactivate.test.tsx` — 65 new tests.

### Changed

* `frontend/src/app/(dashboard)/employees/page.tsx`: the placeholder became the functional directory (`metadata.title = "Employees"`).
* `frontend/src/components/ui/Button.tsx`: accepts a `ref` (React 19 ref-as-prop) so the dialog can move focus to Cancel. No visual or behavioural change otherwise.
* `frontend/tests/routes.test.tsx`: the Employees placeholder assertions were replaced with a check that the route renders the directory; Attendance and System Status remain placeholders.
* `frontend/README.md`, `docs/PHASE4_IMPLEMENTATION_PLAN.md`, `docs/PHASE4_DASHBOARD_PROPOSAL.md`, `DEVELOPMENT_STATUS.md`: document P4.5, the employee-ID compatibility policy, keyset pagination limits and the irreversible deactivation.

### Notes

* Deactivation is guarded by a confirmation dialog that names the employee and states that the API has no reactivation endpoint. Success is displayed only after the response returns; the backend operation stays idempotent.
* Existing employee records whose identifiers violate the proxy policy are listed with detail/rename/deactivate disabled and an explanation. They are not hidden, no proxy rule was relaxed and no operation is claimed to have succeeded.
* Pagination never derives a total from `count` (the page length), offers no search or sorting (the API supports neither), and treats a full final page followed by an empty page as normal.
* No employee fixtures were added: the development fixture gate still covers the Overview only, so live employee failures stay distinguishable from an empty directory.
* No dependency was added, upgraded or removed (15 packages, `npm ls --depth=0` exit 0). No Python, proxy-policy, database, secret, camera or biometric change, and no Git operation. P4.6 and later were not started.

## [Unreleased] — Phase 4, P4.4: typed API client, secure proxy, fixtures and Overview

### Added

* `frontend/src/lib/api/` — the typed contract layer: `types.ts` (interfaces plus `parse*` runtime validators for employees, employee pages, attendance, health and the error envelope, including the nullable `created_at`, the page-length `count` and the top-level `fields` array), `errors.ts` (`ApiError` distinguishing `network`, `http`, `malformed` and `unknown`), `client.ts` (one same-origin URL builder, envelope parsing, abort support), `employees.ts` (keyset paging that follows `next_after`, asks for 100 per page and refuses a repeated cursor or an over-long walk) and `attendance.ts` (the whole journal, with no invented pagination).
* `frontend/src/app/api/backend/[...path]/route.ts` plus `frontend/src/lib/server/proxy.ts` — the same-origin proxy: an allowlist of exactly the eight published method/path pairs, identifier validation and single re-encoding, `after`/`limit` as the only accepted query, bodies only for the two write routes (≤4 KiB) and never for checkout, `redirect: "manual"` so a redirect becomes a sanitized `502`, validated bare-origin `BACKEND_BASE_URL`, and sanitized `502`/`504`/`500` failures that expose neither the origin nor a stack trace. No CORS is added and responses are `no-store`.
* `frontend/src/lib/fixtures/overview-fixtures.ts` and `frontend/src/lib/api/data-source.ts` — development-only synthetic data (six employees, a journal with open and closed sessions and an incomplete trailer) that matches the contract exactly, enabled only by `NODE_ENV=development` plus `DASHBOARD_DATA_MODE=fixtures`, resolved on the server so production builds and browsers cannot enable it.
* `frontend/src/lib/dashboard/overview.ts` — pure metric calculations: totals across all pages, active count, UTC-day event count, latest-event session folding with deterministic tie-breaking, and exclusion/counting of undated and unclassified events.
* `frontend/src/components/dashboard/{OverviewDashboard,MetricCard}` — the functional Overview: four derived figures, a persistent `FIXTURE DATA — DEVELOPMENT ONLY` banner, a journal-integrity warning, loading/empty/error/partial states, keyboard-operable Retry and Refresh, and no fabricated rate, trend, score or chart.
* Tests: `proxy.test.ts`, `api-client.test.ts`, `fixtures.test.ts`, `overview-metrics.test.ts` and `overview.test.tsx` — 99 new tests covering the allowlist and its refusals, redirect and timeout handling, envelope mapping, fixture gating and production refusal, pagination guards, UTC boundaries, session folding and every Overview state.

### Changed

* `frontend/src/app/(dashboard)/overview/page.tsx`: the placeholder became the functional dashboard. The page is `force-dynamic` and resolves the data-source kind on the server, so fixture mode can never be enabled by the client.
* `frontend/tests/routes.test.tsx`: the Overview placeholder assertions were replaced by checks that the route renders the functional dashboard, with fixture mode off by default. The Employees, Attendance and System Status placeholder checks are unchanged.
* `frontend/README.md`, `docs/PHASE4_IMPLEMENTATION_PLAN.md`, `docs/PHASE4_DASHBOARD_PROPOSAL.md`, `DEVELOPMENT_STATUS.md`: document the API layer, proxy policy, fixture gating, metric definitions and the P4.4 results.

### Notes

* Contract facts were read from the backend source, not assumed: event types are lowercase `check_in`/`check_out`, timestamps are UTC ISO-8601, `created_at` is nullable, the employee-list `count` is the page length, and attendance has no pagination.
* No dependency was added, upgraded or removed (15 packages, `npm ls --depth=0` exit 0). Verified: lint exit 0 (0 problems), typecheck exit 0, **184 tests passed / 184 in 12 suites** (from 85), and `next build` exit 0 with dynamic `/overview` and `/api/backend/[...path]`.
* Nothing was verified against a live backend: the FastAPI G2+G8 startup gate requires PostgreSQL, so all evidence comes from mocked responses and development fixtures. No browser was driven.
* No backend, database, secret, camera or biometric access, and no Git operation. P4.5 and later were not started.

## [Unreleased] — Phase 4, P4.3: application shell and navigation

### Added

* `frontend/src/app/(dashboard)/` — the dashboard route group with one shared layout: `/overview`, `/employees`, `/attendance` and `/system-status`, each rendering a clearly labelled structural placeholder, plus `src/app/not-found.tsx` for unmatched paths.
* `frontend/src/components/layout/` — `AppShell` (skip link, sidebar, header, drawer, `<main id="main-content">`), `Sidebar` (persistent desktop navigation with `aria-current="page"`), `Header` (menu trigger with `aria-expanded`/`aria-controls`, current-area label, theme toggle), `MobileNavigation` (modal drawer with scrim, close button, Escape and close-on-navigation) and `PagePlaceholder`, each with a CSS Module.
* `frontend/src/components/icons/NavIcon.tsx` — four inline SVG icons drawn with `currentColor` strokes, replacing the need for an icon package.
* `frontend/src/lib/navigation.ts` — the single source of truth for navigation: `NAV_ITEMS`, `DEFAULT_ROUTE`, `isActivePath` (exact or nested path, never a bare prefix) and `findNavItem`.
* `frontend/tests/` — four new suites: `navigation.test.ts`, `app-shell.test.tsx`, `mobile-navigation.test.tsx` and `routes.test.tsx`, covering landmarks, all four links and their hrefs, active state, skip link, the root redirect, every placeholder route, `aria-expanded`/`aria-controls`, focus movement, Escape, scrim dismissal and a full keyboard path.

### Changed

* `frontend/src/app/page.tsx`: the landing placeholder is replaced by a redirect to `DEFAULT_ROUTE` (`/overview`), so the root is never a shell without a selected area.
* `frontend/src/app/layout.tsx`: reduced to document structure — `<html>`, the pre-paint theme bootstrap and `<body>` — with a metadata title template; the header and `<main>` moved into the shell, and page metadata now supplies the area name.
* `frontend/README.md`, `docs/PHASE4_IMPLEMENTATION_PLAN.md`, `DEVELOPMENT_STATUS.md`: document the routes, the shell, the accessibility behaviour and the P4.3 verification results.

### Removed

* `frontend/src/app/layout.module.css`, `frontend/src/app/page.module.css` and `frontend/tests/home-page.test.tsx` — superseded by the application shell and the routes it hosts. The P4.2 theme and design-token suites are untouched.

### Notes

* No dependency was added, upgraded or removed (15 packages, `npm ls --depth=0` exit 0). Verified: lint exit 0 (0 problems), typecheck exit 0, **85 tests passed / 85 in 7 suites** (up from 56), and `next build` exit 0 with six static routes.
* Two failures were fixed in implementation rather than by weakening checks: `jest-dom`-only matchers (not in the approved stack) were replaced with plain DOM assertions, and a keyboard test now asserts the real tab order instead of assuming jsdom applies media queries.
* No backend, database, secret, camera or biometric access, and no Git operation. No dashboard data, proxy or authentication was implemented.

## [Unreleased] — Phase 4, P4.2: frontend scaffold and design system

### Added

* `frontend/` — the approved Next.js 16 App Router scaffold: `package.json` with every dependency pinned exactly (`next 16.4.6`, `react`/`react-dom 19.3.1`, `typescript 5.9.3`, `@types/node 24.19.2`, `@types/react`/`@types/react-dom 19.3.0`, `eslint 10.14.0`, `eslint-config-next 16.4.6`, `vitest 5.1.0`, `vite 8.3.8`, `jsdom 31.0.0`, `@testing-library/react 16.3.3`, `@testing-library/dom 10.4.2`, `@testing-library/user-event 14.6.9`), a committed `package-lock.json`, `tsconfig.json` (strict), `next.config.ts`, `eslint.config.mjs`, `vitest.config.mts` and `README.md`.
* `frontend/src/app/globals.css`: the design system as CSS custom properties — dark graphite + teal, a light theme that overrides every colour token, spacing, typography, radii, shadows, focus and transition tokens, a global `:focus-visible` ring and reduced-motion support. Every colour token is declared in both theme blocks.
* `frontend/src/lib/theme.ts`: framework-free theme primitives (`isTheme`, `parseThemePreference`, `resolveTheme`, `otherTheme`, `applyTheme`, `readStoredPreference`, `writeStoredPreference`, `systemPrefersDark`) plus `THEME_INIT_SCRIPT`, the pre-paint bootstrap that applies the stored or system theme before first paint so there is no flash.
* `frontend/src/lib/themeStore.ts` and `frontend/src/hooks/useTheme.ts`: the applied theme lives on `<html>` behind an external store read with `useSyncExternalStore` and a `null` server snapshot — no hydration mismatch, no effect-driven `setState`, OS changes followed only while the preference is `system`.
* `frontend/src/app/{layout,page,loading,error}.tsx` with their CSS Modules: a root layout carrying the theme bootstrap and a slim brand header, a landing placeholder that claims no dashboard functionality, a `loading.tsx` status region, and a sanitized `error.tsx` boundary that renders only Next's opaque digest.
* `frontend/src/components/ui/{Button,ThemeToggle}.tsx`: the design-system primitives — a four-variant, two-size button and an accessible, keyboard-operable theme toggle whose pre-mount label is stable.
* `frontend/tests/` — 56 tests over four suites (`theme.test.ts` 10, `theme-toggle.test.tsx` 6, `home-page.test.tsx` 2, `design-tokens.test.ts` 38) covering resolution precedence, persistence, keyboard operation, SSR safety and dual-theme token coverage.
* `docs/PHASE4_IMPLEMENTATION_PLAN.md`: the milestone roadmap (P4.1–P4.9) with the approved dependency set, delivered structure, token inventory, theme architecture, verification results and remaining constraints.

### Changed

* `docs/PHASE4_DASHBOARD_PROPOSAL.md`: rewritten from "proposal only, nothing installed" to the approved direction actually implemented — Next 16.4.6 / React 19.3.1 / TypeScript 5.9.3 / ESLint 10 flat config / Vitest + jsdom + Testing Library, the `frontend/` + `src/app/` layout, the verified Node 24.21.0 runtime, and the verified API contract facts (keyset employee paging, no attendance pagination, no employee name in events, bodiless checkout).
* `DEVELOPMENT_STATUS.md`: added Step 41 recording the P4.2 scaffold, exact versions, theme architecture, measured checks and what remains unbuilt.

### Fixed

* `frontend/src/hooks/useTheme.ts`: the initial implementation set state inside an effect, which the `react-hooks/set-state-in-effect` rule rejects; it was replaced with the external-store pattern rather than suppressing the rule.

### Notes

* `vite` was pinned to **8.3.8**, the version satisfying Vitest 5's required peer range (`^6.4 || ^7 || ^8`) on Node 24. `@eslint/eslintrc` was **not** needed: `eslint-config-next` 16 exports a flat config array directly.
* Verified: `npm ci`, `npm ls --depth=0`, `npm run lint`, `npm run typecheck`, `npm run test` (56/56) and `npm run build` all exit 0. No backend, database, biometric access or Git operation occurred; no dashboard screen, navigation or proxy was built.

## [Unreleased] — Phase 3, Step 40: eight implemented endpoints and S8 offline verification

### Added

* `src/attendance.py` (**additive** operator-authorized check-out): `AuthorizationSource` (`BIOMETRIC_VERIFICATION` / `OPERATOR_AUTHORIZATION`) and `OperatorAuthorization`, a frozen object that *cannot* be constructed as a biometric claim — passing the biometric source or a blank operator raises `AttendanceInputError`, and it carries no status, streak or window, so it is not a `TemporalVerificationResult` in disguise. `AttendanceEngine.operator_checkout()` is the approved additive engine path: it requires that authorization (a missing/`None`/wrong-typed one yields `AUTHORIZATION_REQUIRED` with no event and no state change), then applies **the same policy code** as the camera path — the shared `_refuse_inactive` gate, the open-session adjudication (in-memory marker → `open_session_lookup` hook → honest rejection) and the independent `check_out_cooldown_seconds` window. `AttendanceDecision` gained `reason_code` (stable `REASON_*` values) and `authorization`, populated only on the operator path so every pre-existing biometric decision keeps its exact shape. `update()` is untouched: it still requires a `VERIFIED` temporal result, and no temporal evidence is constructed anywhere.
* `src/api_service.py`: the composition layer the routes must not contain. `EmployeeQueryService` wraps the approved `EmployeeRepository` contract (validated structurally, so a fake can be injected offline while a wrong collaborator is still refused) and adds only the API-shaped questions — "duplicate → 409", "missing → 404", the keyset page envelope with `next_after`, and `EMPLOYEE_INVALID` for a non-positive page size. `AttendanceQueryService` reads through `AttendanceEventStore.load_events`, so the store's validation, torn-tail reporting and typed corruption errors apply unchanged; a corrupt journal is never repaired, rewritten, or presented as an empty history. `AttendanceQueryService.checkout` delegates the whole decision to `AttendanceEngine.operator_checkout`: it resolves the employee (404 stays its own concern), loads the journal once through the validated `load_events`, folds it, replays the open-session marker and the last check-out instant into a per-request engine instance through the engine's own `restore_open_session`/`restore_check_out_anchor` methods (so the cooldown is honoured without a second copy of the rule), supplies the explicit `OperatorAuthorization`, and appends only after the engine accepted. `CHECKOUT_COOLDOWN` joins the business-refusal codes; an unexpected decision status raises rather than being reported as a refusal or a success. `build_default_services` refuses with the connection layer's availability error **before** importing the repository, so the request path also keeps the API's offline import hygiene.
* `src/s8/verification.py`: the **S8** implementation of `S8-CONTRACT 1.2` — authorization gate, positive application-target proof (an explicit target may only restate the configured identity), test-target collision refusal, one connection, read-only established before any SQL verification read (`read_only` request and read-back → one `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` → `transaction_read_only`/`transaction_isolation` confirmed), then identity, server version (integer GUC, PG12 floor), the S2 catalog and catalog-derived ledger evidence, all captured on that one transaction; ROLLBACK and close happen **before** evaluation, which uses the existing G2 and S3 authorities plus the pinned digest and contract version. Catalog presence distinguishes absent / ordinary table / wrong kind, `to_regclass` only corroborates, and disagreement is `BLOCKED — DATABASE UNAVAILABLE`, never a definite failure. The seven approved outcomes are preserved verbatim, with additive reason categories; the concurrency limitation is always in the report; no advisory lock, no `verify_schema_locked`, no write of any kind, no retry.
* `tools/verify_schema_s8.py`: operator CLI. Without `--authorize-live-verification` it refuses with `BLOCKED — AUTHORIZATION REQUIRED` and exit `2` **before** settings are read, so an unauthorized run cannot even parse a DSN. Exit codes `0/2/3/4/5/6/7` map one-to-one onto the seven outcomes; `--report` refuses to overwrite an existing file; nothing is written implicitly.
* `tests/test_s8_verification.py`: 39 offline tests over synthetic connections — authorization, every target-identity refusal, one-attempt accounting, read-only ordering between `BEGIN` and the first catalog read, version floor, every ledger classification including wrong relation kinds and both directions of catalog/`to_regclass` disagreement, empty shipped and empty applied collections, test-marker precedence, pinned digest/version, the cleanup precedence matrix, `BaseException` cleanup, report redaction and the absence of advisory-lock or writing statements.
* `src/api_service.py` (concurrency): check-out runs its critical section (journal load → engine decision → durable append) under one module-level `threading.Lock`, closing the reachable in-process race between two overlapping requests; the second request re-reads the journal after the first append and is refused with `NO_OPEN_SESSION`/`CHECKOUT_COOLDOWN`, appending nothing.
* `tests/test_operator_checkout.py`: 29 offline tests for the corrected checkout architecture — authorized operator check-out (event type, authority marker), missing/`None`/wrong-typed/blank-operator authorization, an authorization that tries to claim biometric verification, unknown employee, inactive employee, no open session (with and without the hook), the check-out cooldown including its exact boundary, duplicate check-out, persistence failure (one attempt, no retry, nothing journaled), the service passing the explicit authorization to the engine, the unchanged biometric check-in decision shape, "operator_checkout takes no temporal verification input", "the service never constructs temporal evidence", and "the route contains no check-out business rules" — plus a deterministic two-thread concurrency test (a gated journal load holds the first request inside the critical section while the second is already running; the second must not issue its load, and exactly one `CHECK_OUT` reaches the journal).
* `tests/test_api_endpoints.py`: 24 offline tests — real `uvicorn` on an ephemeral loopback port, an in-memory repository double that raises the **real** typed repository errors, and the **real** `AttendanceEventStore` over a `tmp_path` journal, covering create/duplicate/invalid, keyset pagination, `PATCH` scope, deactivate, empty and corrupt journals, and every checkout outcome (404, 409 inactive, 409 no open session, 201 after check-in, 422 for a fabricated body).
* `docs/S8_CONTRACT_V1_2.md`: the corrected contract (revision 3) with the mandatory corrections C1–C6, including the resolved contradictions in read-only ordering, ledger relation kinds and target designation, the D-1…D-7 evidence table, the cleanup precedence matrix, the mandatory test matrix and an explicit acceptance review that records the three remaining decisions rather than inventing rules.
* `docs/PHASE4_DASHBOARD_PROPOSAL.md`: the Phase 4 scaffold proposal — Next.js App Router, TypeScript, CSS Modules, CSS custom properties, a typed API client behind a same-origin Route Handler proxy (so FastAPI keeps CORS disabled), the four screens with their real data sources, the exact pinned dependency list, and the explicit statement that **nothing is installed or created**.

### Changed

* `src/api.py`: all eight approved endpoints are implemented (none answers `501` any more). Requests are validated by `extra: forbid` models (`EmployeeCreateRequest`, `EmployeeUpdateRequest`, `CheckoutRequest`), so a client cannot supply `created_at`, an `is_active` on update, or a fabricated checkout event id; failures map to distinct sanitized codes (`EMPLOYEE_EXISTS`, `EMPLOYEE_NOT_FOUND`, `EMPLOYEE_INVALID`, `CHECKOUT_REJECTED` with `EMPLOYEE_INACTIVE`/`NO_OPEN_SESSION`, `ATTENDANCE_UNAVAILABLE`, `PERSISTENCE_UNCONFIRMED`, `PERSISTENCE_REJECTED`). `NOT_IMPLEMENTED_CODE` and the deferred-route docstrings are gone; the approved inventory, the loopback/`--print-bind` contract, the Windows single-process guard and the no-CORS/no-auth boundary are unchanged.
* `src/api.py`: no logic change was needed for the corrected checkout — the route already delegated to the service and already mapped any `CheckoutRejected` to `409 CHECKOUT_REJECTED` with a stable `reason`, so the engine's new `CHECKOUT_COOLDOWN` reason flows through the approved envelope unchanged (`201` only after the durable append). A test now asserts that the route body contains no check-out business rule.
* `tests/test_api.py`: the seven parametrized `501 NOT_IMPLEMENTED` assertions and `test_deferred_endpoint_rejects_arbitrary_body` were replaced by `test_endpoints_are_implemented_and_never_report_not_implemented` (never `501`, only documented codes on failure) and `test_unconfigured_database_is_reported_as_unavailable_not_success` (honest sanitized `503 DATABASE_UNAVAILABLE`), driven by a small unavailable-service double so the accepted `forbid_real_database` tripwire keeps holding. No other assertion in the file was changed or weakened.
* `README.md`: the API section now describes the implemented eight endpoints, the service-layer boundary, the pagination and error-code contracts, the checkout semantics and the unchanged security limitations, instead of "only health is implemented".

### Verified

* Focused: `tests/test_operator_checkout.py` → **29 passed**, `tests/test_attendance_engine.py` → **98 passed** (unchanged, the regression guard for the biometric path), `tests/test_api_endpoints.py` → **24 passed**, `tests/test_api.py` → **24 passed** (combined **175 passed**, exit 0). S8 remains **39 passed**. All offline: no connection, no driver in the API request path, no SQL executed, no camera.
* Full offline suite: `pytest -q` → **2325 passed, 32 skipped in 201.35s** (exit 0), +92 over the Step 39 baseline of 2233; the 32 skips are the unchanged opt-in Tier-2 and platform-specific items.

### Not claimed

* **No live database verification.** No PostgreSQL server was contacted, no migration was applied, no schema, permission, ledger or digest was read from a real database; S8's statements have never run against a server, and PostgreSQL 12+ remains an unverified live prerequisite. S7 live provisioning and S8 live verification stay **NOT VERIFIED — REQUIRES SEPARATE AUTHORIZATION**.
* **The checkout deviation is resolved.** `POST /attendance/{employee_id}/checkout` now runs through the approved additive engine path — `AttendanceEngine.operator_checkout` with an explicit `OperatorAuthorization` — instead of appending directly from the API service. No `TemporalVerificationResult` is constructed, mocked or inferred, operator authorization is represented separately from biometric verification and cannot be constructed as the latter, and the `check_out_cooldown_seconds` window applies again because the engine, not the service, owns that rule. The journal read, the policy decision and the append now run inside one module-level `threading.Lock` (`_CHECKOUT_CRITICAL_SECTION` in `src/api_service.py`): Starlette executes synchronous endpoints in a thread pool, so two overlapping check-out requests could otherwise both observe the same open session and both append a `CHECK_OUT`. The scope is stated honestly as **process-local** (the API is a single process by contract), the store keeps its unchanged cross-process no-exactly-once disclaimer, journal format, validation and fsync durability, and residual open sessions still require manual reconciliation (no automatic reconciliation anywhere).
* **Phase 4 is a proposal only.** No dependency was installed, no `package.json`, lockfile or frontend file was created; the Node/npm runtime was not inspected.

## [Unreleased] — Phase 3, Step 39: G8 S1–S5 contract, catalog probe, evaluator, executor integration and startup gate

### Added

* `src/schema_contract.py`: the immutable declarative contract for **G8** (physical schema compatibility verification, the gap G2 deliberately leaves open). It declares `employee` and `schema_version` with canonical `format_type` spellings (`text`, `boolean`, `integer`, `timestamp with time zone`), nullability, default semantics (`is_active` → `true`, `created_at` → **no** default per approved D2), primary keys, and no required secondary index (the primary-key index already serves the repository's keyset query). It also defines the prohibited-object policy — token-bounded, case-insensitive attendance/embedding/biometric/face/enrollment name patterns plus the exact `_phase3_test_guard` marker, while unrelated tables in `public` stay allowed — and `migration_set_digest()`, a pure SHA-256 over caller-supplied `(version, checksum)` pairs.
* `tests/test_schema_contract.py`: 60 offline tests pinning the contract's structure and immutability, the exact migration-derived expectations, the hard-coded `CONTRACT_MIGRATION_VERSION`/`MIGRATION_SET_DIGEST` (with modified, added, removed and swapped-content migration sets detected), the prohibited-name rules including token-boundary false positives, and the module's no-I/O/no-driver/no-SQL guarantees.
* `src/db_schema_catalog.py`: the **S2** read-only catalog probe — four fixed, schema-qualified `pg_catalog` SELECTs (relations of every relevant kind, real-table columns, table constraints, table indexes) mapped to frozen `RelationFact`/`ColumnFact`/`ConstraintFact`/`IndexFact` records in a deterministically ordered `CatalogSnapshot`. It takes an existing connection and never opens, closes, commits, rolls back or locks, so it executes inside G2's `REPEATABLE READ READ ONLY` snapshot; it reads `format_type`, `attnotnull`, `pg_get_expr` (NULL = no default), `attgenerated`/`attidentity`, constraint columns via `WITH ORDINALITY` and index columns positionally over `indnatts`/`indkey`, and never parses SQL or touches `information_schema` (which is privilege-filtered and would under-report columns for a least-privilege role). It carries no policy, defines no diagnostic code, imports no test support, and raises one fixed-message `CatalogProbeError` with the driver cause preserved, so a failed read can never pass as a smaller valid schema.
* `tests/test_db_schema_catalog.py`: 39 offline tests covering statement safety (SELECT-only, `pg_catalog`-only, no parameters or interpolation, no lifecycle calls), fact mapping (kinds, canonical types, nullability, missing-versus-present default, generated/identity, ordered primary-key and index columns), determinism under shuffled row order, immutability, failure propagation without partial results, sanitized messages, empty catalogs and import hygiene.
* `src/physical_schema.py`: the **S3** pure evaluator. It compares a `CatalogSnapshot` with the S1 contract and returns a frozen `SchemaEvaluation` — a `CompatibilityStatus` (`COMPATIBLE` / `INCOMPATIBLE` / `INDETERMINATE`) and a totally ordered tuple of frozen `Finding`s — with no connection, SQL, filesystem or environment access, no logging and no mutable state. Incompatibility is returned data, never an exception; only an unusable call (non-snapshot value, or an empty, duplicated or self-contradictory contract) raises `PhysicalSchemaInputError`. It enforces table presence and relation kind, column presence/type/nullability/default/generation/identity, ordered primary keys, and the S1 prohibited-name policy applied through `prohibition_for` to every relation kind — with the exact `_phase3_test_guard` marker classified separately as `test_marker`. Missing or contradictory metadata is `SCHEMA_METADATA_INCOMPLETE` (indeterminate), never a match; unrelated public tables, views, indexes, sequences and extra constraints produce no finding. Types are compared against canonical `format_type` spellings only (aliases refused), a required default must normalize to exactly `true`, and generated/identity columns are reported as such instead of as defaults. Findings never echo default expressions, constraint/index definitions, SQL, DSNs or driver text.
* `tests/test_physical_schema.py`: 129 offline tests over hand-built immutable snapshots — positive cases (correct schema, reordered columns, unrelated objects, extra constraints/indexes, normalized `true`), missing tables and every non-table kind, partitioned tables (`INDETERMINATE`), empty/duplicate/incomplete metadata, missing and unexpected columns, type/nullability/default/generation/identity mismatches, ordered primary keys via a two-column contract, every S1 prohibited pattern with marker near-misses and substring false positives, shuffled-input determinism, precedence ordering, verdict semantics, input validation, argument immutability and import hygiene.
* `src/db_migrate_executor.py`: the **S4** executor integration. `verify_schema_locked()` gained keyword-only `catalog_probe` (default `None`) and `physical_evaluator` (default `evaluate_physical_schema`), plus a frozen `LockedSchemaVerification(migration, physical)` result. With no probe the method is unchanged G2 behaviour returning the bare `MigrationSnapshot`; with one it runs one connection, one nonblocking advisory-lock attempt, one `REPEATABLE READ READ ONLY` transaction, the G2 reads, `evaluate(snapshot)`, the catalog probe on that same connection inside that same transaction, the pure S3 evaluation, `ROLLBACK`, the confirmed unlock and the close. The catalog is never probed unless G2 accepts the history, so no G2 refusal can be masked; a catalog read failure becomes a fixed-message `MigrationDriverError` with the cause preserved, as does a failed `ROLLBACK`. The six-method ABC, `read_snapshot()`'s ownership contract, G2's codes and the startup path are untouched. The G2 snapshot reads were split into one shared statement helper, issued in the identical order.
* `tests/test_db_migrate_executor_catalog.py`: 34 offline tests using the real S2 probe and real S3 evaluator against recording fake connections — default lifecycle/return type, combined order, same-snapshot/same-connection/lock-held guarantees, one connection/lock/transaction, G2 precedence (also through the real `evaluate_migration_state`), all three S3 outcomes including partitioned-indeterminate and `test_marker`, catalog/evaluator failures, the rollback/unlock/close cleanup matrix, primary-failure preservation, no writing statements, ABC/signature stability, and source assertions that startup does not activate the gate. (S5 later activated that gate, so those source assertions are replaced by S5's own lifecycle tests.)
* `tests/test_api_physical_gate.py`: 22 offline **S5** tests — the full startup decision matrix (only `COMPATIBLE` allows Uvicorn; `INCOMPATIBLE`, `INDETERMINATE`, catalog unreadable, evaluator failure, unexpected result and cleanup failure all refuse), G2 precedence with no catalog statement after a history refusal, the real S2 probe and real S3 evaluator driven through the real gateway (findings survive the diagnostic boundary, partitioned tables stay `INDETERMINATE` and refuse, prohibited non-table relations stay prohibited, unrelated public objects stay allowed), the one-connection/one-lock/one-read-only-transaction/one-migration-snapshot/one-catalog-snapshot lifecycle with the lock held through evaluation and rollback before unlock before close, guard release on refusal, runner exit code `4` with sanitized findings, `--print-bind` staying database-independent, and import/`create_app()` hygiene.

### Changed

* `src/startup_schema.py`: the gate now enforces **G8 (S5)**. `verify_startup_schema()` supplies the production probe and evaluator to the existing S4 operation (`catalog_probe=read_public_catalog`, `physical_evaluator=evaluate_physical_schema`) and permits startup only when the returned `LockedSchemaVerification` reports `COMPATIBLE`. Two gate-level codes join the existing 13 (`SCHEMA_INCOMPATIBLE`, `SCHEMA_INDETERMINATE`), `SCHEMA_CATALOG_UNREADABLE` is reused from the reserved S3 vocabulary, and `SCHEMA_VERIFICATION_FAILED` covers an unexpected result or evaluator failure. `StartupSchemaError` gained a keyword-only `findings` tuple plus `finding_codes`, carrying the S3 findings unchanged while `str(error)` stays `"<CODE>: <message>"`. A bare `MigrationSnapshot`, an empty findings tuple, an unknown status and an exception are never treated as compatibility; the physical refusal is raised only after the S4 operation has rolled back, unlocked and closed, so cleanup failures keep `MIGRATION_LOCK_RELEASE_FAILED` and G2 history refusals keep their codes and precedence. Catalog unreadability is recognised through the preserved `CatalogProbeError` cause, never as an empty snapshot or an ordinary mismatch, and S4 needed no change.
* `src/api.py`: docstrings only — `run_server()`'s verifier is described as the G2+G8 gate, and `_default_startup_verifier` is documented as the production combined verification. No behaviour, signature or import change.
* `tools/run_api.py`: after the existing sanitized refusal line, a refusal may print at most eight `run-api: schema finding: <CODE> <schema.object[.column]>` lines (stable codes and identifiers only). Exit code stays `4`; `--print-bind` still returns before any verification.
* `tests/test_startup_schema.py`: the shared fake handler now answers the four S2 catalog statements with healthy rows, because the gate also verifies the physical schema. No existing assertion was changed, weakened or removed.
* `tests/test_db_migrate_executor_catalog.py`: one S4 test asserted that startup did **not** yet activate physical verification — the state S5 deliberately reverses. It now pins the active wiring instead (the gate supplies the production probe and evaluator to this combined operation, requires a `LockedSchemaVerification`, and permits startup only on the positive `evaluation.compatible` path), while `src/api.py` and `tools/run_api.py` stay thin. No other S1–S4 assertion changed.
* `DB_DESIGN.md`: new §3.9 records G8 and its owner-approved architecture (one connection and one snapshot shared with G2, explicit contract rather than SQL parsing, unrelated objects allowed, prohibited patterns plus exact marker, fail-closed startup, G2's exit `4`), plus the digest re-pin procedure, the S2 probe contract (`pg_catalog` rationale, determinism, failure behaviour, excluded catalog classes, PostgreSQL 12+ prerequisite), the S3 evaluator contract (three outcome states, the final diagnostic codes, precedence, conservative type/default comparison, deliberate exclusions), the S4 combined single-connection lifecycle with its G2-precedence and same-snapshot guarantees, both final owner decisions (partitioned tables stay indeterminate; prohibited names cover every relation type) and the pending S6–S8 stages; §3.8's out-of-scope note now points at it. The §3.7 `G3` entry (migration-file hardening) is unchanged — G8 is a distinct label.
* `DEVELOPMENT_STATUS.md`: Step 39 record for S1–S5.

### Verified

* Focused: `tests/test_api_physical_gate.py` → **22 passed**; `tests/test_schema_contract.py` → **60 passed**, `tests/test_db_schema_catalog.py` → **39 passed**, `tests/test_physical_schema.py` → **129 passed** and `tests/test_db_migrate_executor_catalog.py` → **34 passed**; the existing G2 suites (`test_startup_schema`, `test_api_startup_gate`, `test_db_migrate_executor`, `test_db_migrate`) → **161 passed** and the G2 API/guard slice (`test_api_guard` + `test_api`) → **38 passed** (all exit 0); fully offline (no connection, no driver import, no SQL executed).
* Full offline suite: `pytest -q` → **1989 passed, 32 skipped in 223.68s** (exit 0), +22 over the S4 baseline of 1967; the 32 skips are 28 unchanged opt-in Tier-2 items and 4 platform-specific items (`test_employee_export.py`: 1 POSIX permission-bit test, 3 symlink-creation tests).

### Not claimed

* **No live database verification.** G8 is now enforced at startup, but only offline against fakes: the probe's SQL has never been executed against a PostgreSQL server, and it reads `attgenerated`/`attidentity`, so PostgreSQL 12+ remains an unverified prerequisite. S6 (audit/documentation completion), S7 (owner-run negative database provisioning) and S8 (separately authorized live read-only verification) remain pending. No database was contacted, no DDL/DML or migration ran, and no dependency, configuration or migration-file change was made. Partitioned tables stay `INDETERMINATE` and refuse startup by final owner decision; prohibited names cover every relation type by final owner decision.

## [Unreleased] — Phase 3, Step 38: G2 startup schema compatibility gate

### Added

* `src/db_migrate_executor.py::verify_schema_locked()`: the additive one-connection operation G2 needs, because `acquire_advisory_lock()` and `read_snapshot()` each own a different connection and the snapshot closes its own. It opens exactly one connection and runs the approved order — `pg_try_advisory_lock` (nonblocking, never retried; `False` is a typed `MigrationContentionError`), the existing `REPEATABLE READ READ ONLY` snapshot **on that connection** (now including the existing ledger-shape query, so a malformed ledger is a typed `MigrationIntegrityError` rather than a driver failure), the caller's evaluation while the lock is still held, `pg_advisory_unlock` whose boolean **must** be `True`, then close. Unconfirmed release, an unlock error, or a close failure is a `MigrationCleanupError`; a refusal already in flight is never masked (G7 scope: `Exception`). The six-method ABC, `read_snapshot()`'s ownership contract, and the migration CLI are unchanged; the snapshot SQL is shared through one private helper instead of being duplicated.
* `src/startup_schema.py`: the G2 gate. `resolve_startup_targets()` requires both `ATTENDANCE_DATABASE_URL` and `ATTENDANCE_TEST_DATABASE_URL`, normalizes both through the existing `src/db_target.py` (so `localhost`/`127.0.0.1`/`::1` are one server), refuses ambiguous or identical targets, and returns the application DSN as the only value ever passed to an opener — the test DSN is compared and nothing else. `evaluate_migration_state()` delegates every existing judgement to `read_state()`/`_reconcile()` and adds only the G2 rules the loader deliberately treats as benign: a non-empty shipped set, a present ledger, a set beginning at V001, and no pending versions. `verify_startup_schema()` maps failures to the 13 approved codes and raises the typed `StartupSchemaError` with a fixed sanitized message (no DSN, credential, SQL text, driver text, path, or traceback).
* `src/api.py::run_server(..., startup_verifier=None)`: the G2 hook runs **after** the single-process guard is acquired and **before** Uvicorn is imported, so a refusal releases the guard and never constructs a server. The default is the real verifier; the keyword is the approved test seam, and there is deliberately no environment-variable bypass or warning-only mode. `create_app()` and importing the module remain database-side-effect-free.
* `tools/run_api.py`: a G2 refusal prints one sanitized `run-api: <CODE>: <message>` line to stderr and exits **4**, distinct from `2` (configuration) and `3` (guard). `--print-bind` still skips the gate and never connects.
* `tests/test_startup_schema.py`, `tests/test_api_startup_gate.py`: 69 new offline tests — the full policy table (allow plus every refusal code), target safety (both DSNs mandatory, malformed/ambiguous refused, equal identities rejected, the test DSN never reaching an opener), a recording factory plus `src.db._connect` tripwire proving every invalid-target category refuses **before** executor construction and before any connect, executor-construction failures inside the sanitized diagnostic path (ordinary exception → `SCHEMA_VERIFICATION_FAILED` with the fixed message and no DSN/secret/traceback, a specific `StartupSchemaError` still propagating unchanged), the one-connection lifecycle order with a recording fake connection, no snapshot after a refused lock, no retry, the cleanup matrix, guard → verification → Uvicorn ordering, guard release on refusal, no production bypass, and the runner's `0`/`3`/`4` codes with sanitized stderr.

### Changed

* `tests/test_api_guard.py`: the two real-server cases inject the approved no-op verifier (they own guard lifetime, not schema compatibility) so the API suite stays database-free; `tests/test_api.py`'s autouse tripwire now checks that psycopg is not imported **by the API test that is running**, because the G2 target check legitimately uses psycopg's DSN parser without connecting — the fresh-subprocess import test still proves `import src.api` alone pulls no driver.
* Documentation: `DB_DESIGN.md` §3.2/§3.3 (startup no longer described as running migrations), §3.7 (G2 gap retired), new §3.8 (gate contract, one-connection lifecycle, codes, out-of-scope physical drift), §6 row 1.5b-7, new §11.8 (verification scope); `README.md` (exit `4`, read-only startup verification, apply-before-serve); `.env.example` (both DSNs required for startup, test DSN comparison-only).

### Verified

* Focused: `tests/test_api.py tests/test_startup_schema.py tests/test_api_startup_gate.py tests/test_api_guard.py` → **107 passed** (exit 0), including the real-subprocess guard/lifetime cases.
* Full offline suite: `pytest -q` → **1705 passed, 32 skipped** (`1705 passed, 32 skipped in 219.93s`, exit 0). The 32 skips are the same pre-existing opt-in Tier-2 modules; no previously passing test started skipping and no new skip was introduced.

### Not claimed

* **No application-database verification was performed.** `ATTENDANCE_DATABASE_URL` was never connected to, no migration ran, no DDL/DML was executed, and no Git, dependency, or configuration-file change was made. The gate is offline-contract-verified only; the separately authorized read-only live check remains outstanding.

## [Unreleased] — Phase 3, Step 1.5b-6: real migration executor and migration CLI

### Added

* `src/db_migrate_executor.py`: the real psycopg-backed `PsycopgMigrationExecutor`. It requires an explicit, non-blank DSN (there is deliberately no fallback to `ATTENDANCE_DATABASE_URL`, which is how a mis-targeted run would reach the wrong database), opens nothing at construction, and owns one dedicated session connection for a nonblocking `pg_try_advisory_lock` held across the whole run — a refused acquisition closes the connection and holds nothing, and release is idempotent and raises only after closing. The ledger is created only when `to_regclass` reports it absent; an existing ledger's documented column structure is validated from `information_schema.columns` and any divergence is a typed `MigrationIntegrityError`, never a repair. Each migration's SQL is executed verbatim and parameterless (so psycopg's simple-query path runs the file's statements inside the executor's transaction), then its ledger row is inserted, then one commit — any failure rolls the whole transaction back. `read_snapshot()` is the one method the ABC does not carry: a single `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` transaction reads tables, ledger presence and ledger rows from one MVCC snapshot, then rolls back and closes, so `status` never sees a table list and version list from different points in time. Availability and DSN errors propagate unchanged; unexpected driver failures become fixed-message `MigrationDriverError`s with `__cause__` preserved, and cleanup never masks a primary exception it handled (G7: scoped to ordinary `Exception`s).
* `src/db_target.py`: `normalized_target_identity()` and `same_target()`. Identity is dbname/host/normalized-port with textual local aliases collapsed and credentials excluded; `hostaddr`, `service`, a missing host, a missing database name, or a non-numeric port yield **no** identity, so ambiguity can never masquerade as proof that two targets differ.
* `src/db_migrate.py`: `MigrationSnapshot`, `MigrationState` and the pure `read_state(snapshot, migrations_dir=None)`, appended without changing `migrate()`, `_reconcile()`, checksum or script validation, `ADVISORY_LOCK_KEY`, V001, the six-method ABC, or `FakeExecutor`. `read_state()` reuses `load_migrations()` and `_reconcile()` for every judgement (checksum drift, duplicate rows, gaps, applied-but-missing scripts, `employee` without a ledger) and refuses a snapshot that claims ledger rows without a ledger.
* `tools/run_migrations.py`: `status`, `status --json` and `apply`. The target is the configured application DSN only; it refuses a target whose normalized identity equals the configured test DSN and fails closed when a configured test DSN cannot be normalized — there is no `--allow-test-target` escape. `status` uses only `read_snapshot()` and `read_state()` (no lock, no ledger creation, no writes). Exit codes: `0` success, `1` unexpected internal error, `2` configuration/target refusal, `3` lock contention, `4` database unavailable/driver failure, `5` migration refused or failed, `6` run completed but lock cleanup failed. Output names the target as `dbname=… host=… port=…` and never prints a DSN, user, password, or driver text.
* `tests/test_db_target.py`, `tests/test_db_migrate_executor.py`, `tests/test_run_migrations_cli.py`: 82 fake-driven Tier-1 tests covering identity normalization, statement text and bound parameters, presence-before-CREATE ordering, four divergent ledger shapes, rollback on failed script and failed ledger insert, the close/rollback non-masking matrix, lock lifecycle, the single read-only snapshot, `read_state()`'s delegation, every exit code, credentials-free output, and import hygiene.
* `tests/tier2_support.py`, `tests/test_tier2_migration_executor.py`: the opt-in live harness. It binds every executor to the guard-verified test DSN (`bind_target_to_dsn`, with explicit refusals for an unverified target, a blank DSN and a different database), fingerprints the catalog read-only, and asserts inventory equality before and after each scenario.

### Verified

* Full offline suite: **1629 passed, 31 skipped** (`1629 passed, 31 skipped in 225.87s`, exit 0), the 31 skips being the two opt-in Tier-2 modules (17 pre-existing + 14 new).
* Live Tier-2, read/lock only: `ATTENDANCE_TIER2_OPT_IN=1 ./.venv/Scripts/python.exe -m pytest tests/test_tier2_migration_executor.py -m tier2 -v` → **14 passed, 0 failed, 0 skipped** against `visual_attendance_tier2_test` (`localhost:5432`), with the unmodified guard reporting `READY`. Observed: one consistent snapshot whose ledger rows equal the catalog baseline, `ensure_ledger()` as a no-op under a role without `CREATE`, `migrate()` as a no-op on a fully applied target, advisory-lock contention and release, a deliberately failing migration that committed nothing and left no `version = 999` row, and unchanged catalog inventory (relations, columns, constraints, indexes, ledger rows, employee count, guard rows). `ATTENDANCE_DATABASE_URL` was never connected to.

### Not claimed

* No committed migration, no DDL/DML, no employee/ledger/guard-row change, and no cleanup delete occurred. `apply` against a live schema and `ensure_ledger()`'s CREATE path remain fake-verified only. G2 (no startup enforcement), G3 (migration files are validated, not hardened) and G7's scope are documented in DB_DESIGN.md §3.7.

## [Unreleased] — Phase 3, Step 1.5c-3b: live Tier-2 PostgreSQL verification executed

### Verified

* `tests/test_tier2_employee_repository.py` ran for the first time against a real PostgreSQL instance: the owner-provisioned, dedicated `visual_attendance_tier2_test` database, reached only through `ATTENDANCE_TEST_DATABASE_URL` as the least-privilege role `attendance_tier2_runtime`. `ATTENDANCE_DATABASE_URL` was never connected to and reached only the guard's distinctness proof. Command: `ATTENDANCE_TIER2_OPT_IN=1 ./.venv/Scripts/python.exe -m pytest tests/test_tier2_employee_repository.py -m tier2 -v` — **13 passed, 0 failed, 0 skipped, 0 errors** in 12.02s. This is live verification and does not replace or expand the offline suite's results.
* The unmodified `src/test_database_guard.py` preflight reported `READY`: declared and connected identity matched the dedicated test target and differed from the application's, server-side read-only was enforced before any verification query, `_phase3_test_guard` held exactly one row with the approved literal and a timezone-aware `created_at`, and its claimed `schema_version` (1) matched the migration ledger's recorded version (1).
* Live scope actually exercised, because nothing skipped: real connection open/close, PostgreSQL refusing a write in a read-only transaction, dry-run classification, `--apply` creating the synthetic employee, identical repeat, each differing field (`full_name`, `is_active`, `created_at`) as a never-updated conflict, duplicate create → `EmployeeAlreadyExists`, create race → conflict, failed transaction leaving no state with a reusable connection, typed not-found on update without writing, and the spy proving the importer never updates or deactivates.
* Guarded cleanup left **zero** synthetic rows: the harness's teardown asserts that and would have failed the run otherwise, and a post-run read-only count showed `public.employee` at 0 rows total with 0 rows in the harness's `T2-` namespace. The harness's per-run `deleted`/`absent` counters are exposed only through `report.left`, so **separate deleted/absent counts were not observed and are not claimed**.

### Changed

* No production code, test, schema, migration, dependency, or configuration file changed in this verification step: the committed harness, guard, and repository wiring ran unmodified, and the opt-in switch was set inline for the single invocation rather than persisted to `.env` (where the key is absent, so it can no longer be inherited from the file).

### Not claimed

* No production-readiness claim, and no real-camera, real-employee, photo, embedding, or attendance-record validation: only the disposable test database and its synthetic namespace were touched. The ledger and marker rows remain at one each, and the earlier exposure of the test role's password in a pytest assertion still stands as a rotation recommendation.

## [Unreleased] — Phase 3, Step 1.5c-3b-prep: Tier-2 verification harness prepared

### Added

* `tests/test_tier2_employee_repository.py`: the opt-in Tier-2 PostgreSQL verification harness. It cannot run in the ordinary offline suite — without the exact `ATTENDANCE_TIER2_OPT_IN=1` per-shell setting every item skips itself at collection with a reason naming the switch (proven in a fresh subprocess), and a missing prerequisite is reported as a skip with a typed reason, never as a pass. With the opt-in on, it refuses to start unless both `ATTENDANCE_TEST_DATABASE_URL` and `ATTENDANCE_DATABASE_URL` are configured, then runs the unmodified `src/test_database_guard.py` ordered positive verification and skips with the guard's typed reason on any blocked outcome — no guard check is weakened, bypassed, or re-implemented. Every repository is built with an explicit `dsn=` (the application DSN reaches only the guard's distinctness proof, never the repository); prepared scenarios cover connection open/close, server-side read-only write refusal (`sqlstate == "25006"`), dry-run classification, apply creating the synthetic employee, identical repeats, each differing field as a never-updated conflict, duplicate creates as `EmployeeAlreadyExists`, create races reported as conflicts, rollback/connection cleanup, and a spy proving the importer never updates, deactivates, or enumerates. Synthetic rows live in a per-run `T2-<8-hex>-<slot>` namespace with a fixture tracking intent, and the harness's only write is guarded cleanup: `require_destructive_action` against an explicit one-entry manifest before connecting, only harness-shaped IDs, deletion restricted to rows whose stored `(full_name, is_active, created_at)` equals exactly what the run wrote, one bound-parameter `DELETE … = ANY(%s)` (no `LIKE`, no DDL), `deleted`/`absent`/`left` accounting, and closing the connection on every path. Rows whose ownership cannot be positively established are left untouched.
* `tests/test_tier2_harness_control.py`: 25 fake-driven offline harness-control tests — opt-in semantics, pre-flight ordering (no opener run on a missing input), skip-with-typed-reason, repositories built only with the explicit guarded test DSN, refused direct `VerifiedTestTarget` construction (genuine targets produced through the real guard over an injected connection with a stdlib DSN parser, keeping the shared process psycopg-free), cleanup manifest/ownership/shape gating with exact-match-only deletion (sibling prefix IDs never eligible and connection closed on success and failure), and an AST scan proving every string literal in the harness is free of `INSERT`, `UPDATE`, `CREATE`, `DROP`, `ALTER`, and `TRUNCATE`.

### Changed

* `pytest.ini`: registered the `tier2` marker so the opt-in skips are explicit configuration rather than unknown-mark warnings.
* `.env.example`: documented `ATTENDANCE_TIER2_OPT_IN=1` next to the test DSN.

### Security notes

* No PostgreSQL connection, DSN value read, SQL execution, migration, marker write, or Tier-2 scenario occurred in this step: the harness is **prepared, not executed**, and every test runs offline against fakes or a skipped subprocess. The database, its `_phase3_test_guard` marker row, and its migration ledger remain owner-provisioning work; no variable in this step may ever point at a shared or production database.
* **Not verified, and not claimed:** server-side read-only refusal, real connection lifecycle, marker evidence, a live import, and guarded cleanup against real rows.

## [Unreleased] — Phase 3, Step 1.5c-3a: PostgreSQL employee-repository wiring

### Added

* `src/db_employee_repository.py`: the production wiring that binds the approved `EmployeeRepository` to the lazy connection layer (`src.db.open_connection`). It contains **no SQL** and no second persistence implementation — statement ownership and the typed error taxonomy stay in the repository. `open_employee_repository(dsn=None, *, read_only=False, connection_opener=None)` opens nothing at construction; the first repository call does. `src.db` (and psycopg) is imported lazily inside the default opener, so importing the module loads no driver and reads no DSN. Each operation gets a fresh connection whose complete lifecycle the scope owns: open, optional read-only enforcement (requested **and read back** before any statement, refused fail-closed if unavailable), the repository's single `execute`, `commit()` on success, `rollback()` plus unchanged re-raise on failure, and `close()` on every path. Cleanup never masks a real failure: a rollback error is swallowed while an exception is propagating, a close error is attached as a note to that exception, and a close error with nothing else in flight is raised. `connection_opener` is a documented offline-test seam only.
* `tests/test_db_employee_repository.py`: 38 offline tests driving the real repository SQL through an in-memory psycopg-3-shaped fake server (rows, `sqlstate`-carrying violations, settable/readable `read_only`, observable commit/rollback/close) — construction opens nothing, statement text and bound parameters are the approved ones, `23505` becomes `EmployeeAlreadyExists` while `23502` and sqlstate-less errors do not, read-only is requested before the first statement, writable use never requests it, every failure path rolls back and closes (including rollback- and close-failure masking, note attached rather than replacing), availability errors propagate as the same object, the DSN reaches the opener verbatim while `None` stays the connection layer's resolution, a non-boolean `read_only` is refused, the module defines no SQL and has no module-level driver import, a fresh-process check proves importing it loads neither `src.db` nor psycopg, and the importer is exercised end-to-end over the wiring (dry run reads and never inserts, `--apply` inserts only missing rows with the artifact's `created_at`, conflicts and create races never write, an unavailable database stops the run with only the exception type in the report).

### Changed

* `tools/import_employee_metadata.py`: repository-backed modes now build the production wiring lazily (`_default_repository_factory(apply=...)` → `open_employee_repository(read_only=not apply)`). The default dry run therefore classifies against PostgreSQL over a **server-enforced read-only** connection, and `--apply` is the only mode that gets a read-write connection (approved creates only). `--validate-only` still never imports the wiring and needs no DSN, driver, or connectivity. The Step 1.5c-2 refusal of both repository-backed modes is superseded; a direct runner call with no repository at all still refuses (exit 2) before any database access. Help text, mode text and the `--apply` description were updated to match.
* `tests/test_employee_import.py`: 96 → 102 tests. Six new tests cover wiring selection (`--validate-only` never builds it; dry run requests read-only; `--apply` requests read-write; an injected factory wins; the mode→read-only mapping is exact; importing the CLI loads neither the wiring nor psycopg). Two obsolete "missing wiring" tests were renamed and rescoped to the runner's defensive no-repository refusal — replaced because the CLI now always supplies the wiring, not weakened.

### Security notes

* No PostgreSQL connection, DSN read, SQL, migration, DDL, or Tier-2 verification occurred in this step or its tests: every path runs against fakes and temporary directories. Read-only mode is requested and positively verified **before** the first statement; a connection that cannot be set or does not report read-only is refused rather than queried. Errors and reports expose only exception **type names** and employee IDs — never DSNs, credentials, driver text, or names.
* **Not verified, and not claimed:** PostgreSQL's server-side refusal of a write in a read-only transaction, real psycopg lifecycle behaviour, live DSN resolution, and the real import. The wiring is offline-tested only; the Tier-2 run against the owner-provisioned dedicated test database remains a separate authorization.

## [Unreleased] — Phase 3, Step 1.5c-2: offline employee-metadata importer

### Added

* `src/employee_import.py`: importer for the Step 1.5c-1 JSONL artifact, with no database access of its own. Strict validation of the exact five-key contract (JSON object, all five keys and no others, integer `schema_version` 1, employee ID/name validated through the `Employee` model with normalization-changing values **rejected**, real bool `is_active`, timezone-aware ISO-8601 `created_at` preserved as supplied — never import time); typed rejections carrying line number and readable ID only; duplicate IDs reject every occurrence; records processed in `employee_id` order. Reconciliation is restricted to a two-method repository protocol (`get`, `create`): create when absent, skip when identical (all four stored fields match), report a conflict listing the differing field **names** and never update, treat `EmployeeAlreadyExists` during create as a conflict with no retry, and stop safely on any other repository error while recording only the exception's type name. No name-update or deactivation path exists, so a conflict can never become a write and `created_at` can never be modified. Modes: `validate-only` (no repository), `dry-run` (classify, write nothing), `apply` (creates only). Reports are JSON containing counts, employee IDs, differing field names, line numbers and reason codes only — no names, timestamps, record contents, DSNs, or driver text.
* `tools/import_employee_metadata.py`: operator CLI (`--input`, `--validate-only`, `--apply`, `--report`, `--overwrite-report`) with exit codes `0` clean, `1` rejections/conflicts/stopped run/report failure, `2` CLI or configuration refusal. The psycopg-backed runtime wiring belongs to Step 1.5c-3, so both repository-backed modes are refused (exit 2, before any database access) until that wiring exists; `--validate-only` remains available. Reports are written atomically to a temporary file, fsynced, permission-restricted where supported, and never replace an existing report without explicit overwrite (refused before any work starts).
* `tests/test_employee_import.py`: 96 offline tests using an in-memory repository fake and synthetic JSONL — dry run mutates nothing, apply creates with the supplied timestamp, identical rows skip, each differing field is a conflict with no mutation, create races become conflicts without retry, invalid records reject while valid ones continue, duplicates reject every occurrence, unsupported versions/missing or extra keys/malformed or non-UTF-8 input/normalization changes all reject with typed codes, repository errors stop safely, reports and CLI output leak no names or timestamps, report refusal/overwrite/atomic-failure behavior, every exit code, and a behavioral proof that `update_full_name`, `deactivate`, and `list` are never called.

### Security notes

* No PostgreSQL connection, DSN access, SQL, migration, or Tier-2 verification occurred in this step or its tests; the repository is injected and every apply path is exercised against an in-memory fake only. The real import remains Step 1.5c-3, which additionally requires the owner-provisioned dedicated test database, its marker, and the runtime wiring.
* The importer reads only the export artifact: no enrollment manifest, face sample, embedding, attendance journal, camera, or real employee data is accessed. Conflicts are reported by employee ID and differing field names only; record contents (including names and timestamps) never appear in console output or reports.

## [Unreleased] — Phase 3, Step 1.5c-1: employee-metadata export from enrollment manifests

### Added

* `src/employee_export.py`: the read-only export half of the deferred employee-metadata importer. Scans the Step 07 enrollment manifests (`<DATA_DIR>/employees/<encoded-id>/employee.json`, `format_version: 1`) and emits one versioned JSONL record per employee — exactly five keys (`schema_version` 1, `employee_id`, `full_name`, `is_active`, `created_at`). The employee ID is taken from **inside** the manifest and only checked against the writer's own encoding (`safe_dir_name`); ID and name normalization come from the `Employee` model and any value the model would change is **rejected, never normalized**; `is_active` must be a real bool; `created_at` must be tz-aware ISO-8601 and is preserved **verbatim** (never replaced with export time). Every invalid input produces a typed, name-free `ExportRejection` (17 codes covering malformed/unsupported/incomplete/inconsistent manifests, directory mismatch, normalization changes, duplicate IDs). Only `employee.json` files that are direct children of the manifest root are opened; symlinked entries, symlinked manifests, and a symlinked root are refused, and the `samples` array, `sample_count`, face-sample PNGs, embeddings, and the attendance journal are never read or exported. Output is written to a temporary file, fsynced, permission-restricted to owner-only where supported, and atomically replaced; an existing artifact is refused unless overwrite is explicit; fail-closed by default (any rejection → no artifact) with an explicit partial opt-in; an empty manifest root writes an empty artifact while a non-empty root with no valid record writes nothing.
* `tools/export_employee_metadata.py`: operator CLI over the same path (`0` success, `1` fail-closed/partial/write failure, `2` existing-artifact refusal). Its report carries counts, the artifact path, directory names, and reason codes only — never an employee name.
* `tests/test_employee_export.py`: 94 offline tests (fakes, synthetic manifests, temporary roots) covering the exact five-key schema, verbatim timestamps (`+00:00` and `Z`), non-ASCII names, deterministic ordering, every rejection code, symlink refusal (real symlinks where the platform allows, branch-simulated on Windows), overwrite protection, atomic-write failure, POSIX owner-only permissions, fail-closed/partial/empty-root behavior, source-manifest immutability, sample/embedding non-touch, and the CLI's exit codes and name-free output.

### Changed

* `src/config.py`: new derived `AppSettings.exports_dir` (`<DATA_DIR>/exports`), deliberately **not** created by `ensure_directories` — the export root holds personal data and exists only after an explicit export.
* `.gitignore`: `data/exports/**` is ignored (the artifact contains employee names).

### Security notes

* The export artifact is personal data (employee names). It is git-ignored, owner-permission-restricted where the platform supports it, and must be disposed of when no longer needed; the CLI never prints its contents.
* No PostgreSQL connection, DSN access, database operation, camera access, or biometric/runtime-data access occurred in this step or its tests; every manifest in the tests is synthetic and confined to a temporary data root. The importer itself, the real import, and all API endpoint work remain later authorized steps.
* Employee metadata only: no biometric, embedding, image-path, or attendance-event data enters the export, and PostgreSQL continues to store employee metadata only.

## [Unreleased] — Phase 3, Step 1.5b: employee-metadata persistence foundation (offline)

### Added

* `src/db_migrate.py`: the schema-migration runner core per the approved OQ-1..4 contract — deterministic `V<N>__<name>.sql` discovery with checksum (SHA-256) loading, a fixed documented advisory-lock key acquired via the executor before any state check (fail-fast typed refusal, never inference, never waiting; session-scoped per the owner's H-3 decision below), one ledger row per applied migration (`schema_version(version, checksum, applied_at)`; the runner materializes its own ledger, migrations never do), checksum re-verification of already-applied migrations (a modified applied script is refused, never silently skipped), gap/missing-script/ledger-integrity detection (including the "employee table without a ledger row" refusal), per-migration-one-transaction application through an injected executor contract, and a fully typed sanitized error family (no paths, no SQL text, no driver text; causes preserved for trusted-only diagnostics).
* `migrations/V001__employee.sql`: the approved employee-metadata table verbatim (`employee_id TEXT PRIMARY KEY`, `full_name TEXT NOT NULL`, `is_active BOOLEAN NOT NULL DEFAULT TRUE`, `created_at TIMESTAMPTZ NOT NULL` with **no database default** — application-authored). No biometric/embedding/attendance-event tables ever enter PostgreSQL. Static SQL only; `CREATE DATABASE`/`CREATE INDEX CONCURRENTLY` are prohibited by the runner (non-transactional statements would break commit atomicity).
* `src/employee_repository.py`: the approved metadata-only repository — `create` (duplicates detected explicitly as typed `EmployeeAlreadyExists`, mapped to the reserved API 409 path), `get` (`None` for the caller's 404), `list` (keyset-only `employee_id > after` pages; default 20, hard clamp 100 via new frozen `PaginationConfig` constants in `src/config.py`; no cross-request snapshot claim), `update_full_name` (validation through the existing model before any DB call; `employee_id`, `is_active`, `created_at` untouched — the read-back row is returned), and `deactivate` (persisting status only; inactivity enforcement stays downstream). Database-availability errors propagate untouched; the injected connection source is never used at import/construction time.
* `src/test_database_guard.py`: the Tier-2 positive-verification guard per the approved OQ-5 contract — fixed marker table `_phase3_test_guard` with the fixed literal `VISUAL_ATTENDANCE_PHASE3_TEST_ONLY`, both DSNs required, normalized identity comparison (host alias / user differences can neither fake distinctness nor enable it), connection identity read from the connection object itself, read-only discipline until every ordered check passes, the marker's schema claim cross-checked against the applied migration ledger, a destructive-action manifest gate, and a typed `GuardBlocked` fail-closed result for every missing/ambiguous/inconsistent condition. The DSN parser is injectable so offline tests keep the shared pytest process driver-free (no psycopg import); the default parser uses `src.db` (proven in fresh subprocesses).
### Fixed (independent-audit follow-up)

* `src/employee_repository.py`: duplicate employees are now classified from **psycopg 3's SQLSTATE** (`sqlstate == 23505`) instead of psycopg2's `pgcode`, an attribute psycopg 3 does not have. Before this fix the typed `EmployeeAlreadyExists` / API-409 path could never trigger against the pinned driver, and the test fake masked it by manufacturing `pgcode`. Non-`23505` SQLSTATEs and exceptions without a SQLSTATE propagate untouched.
* `src/db_migrate.py`: the prohibited-statement scan now also refuses `CREATE UNIQUE INDEX CONCURRENTLY` (previously only the bare form) and matches on comment-stripped, whitespace-collapsed text, so case, newlines, tabs, and `/* … */` comments cannot split the keyword sequence. Migration checksums became line-ending independent: the digest is SHA-256 over canonical text (BOM dropped, CRLF/lone-CR normalized to LF), so a CRLF checkout (`core.autocrlf=true` here) can no longer look like a modified migration.
* `src/test_database_guard.py`: identity comparison now fails closed on ambiguity (`hostaddr`, `service`, missing host, missing database name → `IDENTITY_AMBIGUOUS`), compares the driver-reported connected host/port against the declared target (`CONNECTED_TARGET_MISMATCH`), treats an omitted port as libpq's 5432, **enforces** read-only on the connection before the first query (`READ_ONLY_UNAVAILABLE` otherwise), always closes the connection without committing, distinguishes marker-absent / marker-read-failure / multiple-rows, and refuses direct construction of `VerifiedTestTarget` plus malformed manifests (`MANIFEST_INVALID`). This is documented as a mistake-guard library contract, not a security boundary.
### Tests

* 126 offline tests across the three new suites: `tests/test_db_migrate.py` (53), `tests/test_employee_repository.py` (25), `tests/test_test_database_guard.py` (48) — all fakes, temporary directories, and synthetic identities; fresh-process import-hygiene coverage for each new module. The audit-follow-up cases (psycopg-3 error shape, every prohibited-DDL bypass, ambiguous identities, read-only ordering, connection closing, LF/CRLF checksum stability, forged targets) are included in those counts.
### Changed (owner decision — audit finding H-3)

* **OQ-1 lock mechanism replaced: Option B implemented.** The owner approved superseding the original transaction-scoped `pg_try_advisory_xact_lock` — which the audit showed is released before the per-migration transactions it is meant to protect — with a **session-scoped, nonblocking `pg_try_advisory_lock` held on a dedicated connection for the entire run**. `src/db_migrate.py` now acquires it before any state check (ledger creation included), holds it across every per-migration COMMIT, and releases it from one `finally` path covering success, typed failures, unexpected exceptions and a failed acquisition; the executor contract gained `release_advisory_lock()` (release + close, idempotent and no-op safe) and a documented ownership split (executor owns the dedicated connection, runner owns its lifecycle). A release failure is reported without masking the original error — a note when something else is propagating, a typed `MigrationCleanupError` when nothing else failed. **Per-migration transaction and ledger semantics are unchanged**; Option A (one outer transaction) was rejected because it would collapse them. 13 new offline lifecycle tests (`tests/test_db_migrate.py` 40 → 53).
### Security notes

* PostgreSQL stores employee **metadata only**; the JSONL journal remains the sole authoritative attendance journal; no journal access, camera, enrollment-artifact access, or real employee/biometric data occurs in this step or its tests.
* No PostgreSQL server was connected to in this step: all database behavior is the offline-tested contract; the real psycopg-backed executor, the no-serve-during-migration wiring, and Tier-2 integration verification remain later authorized steps (owner must also provision the dedicated test database and establish the marker). The blank-DSN refusal and Gate 1.2 redaction discipline are unchanged and reused.
## [Unreleased] — Phase 3, Step 1.4: Windows single-process startup guard

### Added

* `src/api_guard.py`: Windows-only startup guard — an OS-enforced exclusive `msvcrt.locking` byte-range lock on one byte at offset 0 of `DATA_DIR/api_guard.lock` (path derived from validated `AppSettings.data_dir`). Opened `a+b`; the initial byte is written only when the file is empty, so repeated startups never grow the file. The handle is held for the guarded server's entire lifetime; orderly shutdown releases it, and abnormal process termination is covered by the OS releasing the byte range with the process (a stale guard file never blocks a later startup). Contention/acquisition failures raise the typed `StartupGuardError`; non-Windows platforms raise `StartupGuardUnavailableError` deterministically (no POSIX fallback). Exception messages are fixed sanitized constants.
* `run_server` guard lifecycle: acquisition happens before any request can be served and before Uvicorn is imported (minimal edit in `src/api.py`, disclosed because that file owns `run_server`); the release is the context-manager exit on orderly shutdown. Direct library callers of `run_server` get the same protection; `--print-bind` and app construction never acquire the guard.
* `tools/run_api.py`: guard failures surface as the documented exit code 3 (distinct from configuration exit code 2) with a single sanitized one-line diagnostic — no traceback, no paths.
* `tests/test_api_guard.py`: 14 hardware-free tests using per-test temporary `DATA_DIR` values and real subprocesses — contention across processes, denial while serving, acquisition after orderly release and after abrupt termination, stale-file tolerance, idempotent lock bytes, Uvicorn-never-starts proof, sanitized exit 3, `--print-bind` guard-freedom, and a real graceful-Uvicorn-shutdown release; children are always killed and reaped in `finally`.

### Security notes

* The guard is a **local single-process development safeguard**, not authentication and not a production security boundary; it prevents two API processes serving concurrently on one machine and nothing more.
* Non-Windows platforms fail explicitly rather than silently substituting a weaker Python-level lock; POSIX locking support remains out of scope for Phase 3.
* No database connection, schema change, migration, journal access, camera, or biometric/real-runtime-data access occurs in this step (test-enforced hygiene).

## [Unreleased] — Phase 3, Step 1.3: Loopback-only API foundation

### Added

* `src/api.py`: FastAPI application foundation serving exactly the approved eight `/api/v1` endpoints; only `GET /api/v1/health` is implemented, while the seven non-health approved routes exist at their approved methods and answer a typed `501 NOT_IMPLEMENTED` envelope (no business decision, no database reach, no attendance claim) until their authorized later step. Documentation URLs (`/docs`, `/redoc`, `/openapi.json`) are disabled so the served inventory is exactly the approved eight. No CORS or authentication middleware is installed.
* One stable client-facing error envelope (`{"error": {"code", "message"}}`) with machine-readable codes separate from human messages: `HTTP_ERROR` (registered against Starlette's base `HTTPException`, so framework 404/405 responses use the same sanitized envelope), `REQUEST_INVALID`, `INTERNAL_ERROR` (500 logging trusted diagnostics server-side only), plus `CONFIG_INVALID` / `DATABASE_UNAVAILABLE` / `DATABASE_CONFIG_INVALID` handlers reserved for the database-backed step; raw exceptions, tracebacks, DSNs/credentials, and internal paths never reach responses.
* `tools/run_api.py`: settings-driven single-process development server entry point; binding uses the already-validated `AppSettings.api_host`/`api_port` (never duplicated or bypassed). Non-loopback/invalid `API_HOST`/`API_PORT` fails fast with exit code 2, a sanitized one-line diagnostic, no traceback, and no server start; `--print-bind` prints the validated bind without serving.
* `tests/test_api.py`: offline, hardware-free verification — fresh-subprocess import hygiene (no psycopg/CV/attendance/camera/uvicorn imports and no connection on import or app construction), exact-eight route inventory, ephemeral-loopback real-uvicorn tests over stdlib HTTP with a DB-connection tripwire, sanitized 500 with a credential-bearing synthetic exception, typed-code handler contracts, and runner CLI behavior.

### Security notes

* The API binds loopback-only by validated configuration; loopback binding is a development safety measure, not a production security boundary. There is no authentication and no CORS in this step; any network exposure requires an approved auth/RBAC design in a later phase.
* Nothing in this step connects to PostgreSQL, changes the schema, runs migrations, touches the attendance journal, or accesses camera/biometric data. Importing or starting the API performs no side effects (test-enforced).

## [Unreleased] — Phase 2, P2-6: Check-out event support, journal-backed session recovery, checkout-aware reporting

### Added

* `EventType.CHECK_OUT` (serialized value `check_out`) in the unchanged five-key journal envelope at the unchanged schema version 1; accepted check-outs are persistable through the existing store like check-ins.
* `AttendanceConfig.check_out_cooldown_seconds = 300.0` (validated like the existing cooldowns; `check_in_cooldown_seconds` untouched). The value is an engineering default, not a calibrated organizational policy.
* `src/attendance.py` `AttendanceEngine`: three independently capacity-bounded maps (check-in anchors, check-out anchors, open-session markers), open-session decisions (accepted checkout closes the session and starts the independent checkout cooldown without clearing the check-in anchor; checkout without a known open session is rejected eventless and stateless), and the additive `open_session_lookup` constructor hook for durable adjudication of in-memory misses (strict Boolean contract; typed error on non-Boolean; exceptions propagate; never a silent `False`; never cached). No automatic checkout trigger exists.
* `src/attendance_recovery.py`: pure `fold_latest_events(events)` (per-employee max of `(occurred_at, journal_position)`; equal timestamps by later journal position; UTC datetime comparison; deterministic), shared by `recover_session_state` (open-session/check-out-anchor recovery off the folded latest event), the pre-existing check-in anchor recovery (unchanged contract), and `make_journal_open_session_lookup(store)` (fresh validated `load_events()` per invocation; corruption/I-O errors propagate, never coerced to `False`). Additive engine restoration APIs `restore_open_session` / `restore_check_out_anchor` follow the established P2-3 restoration contract.
* `src/integration_demo.py` durable paths: `recover_attendance_startup_state` performs **one** validated journal snapshot feeding both the P2-3 check-in-anchor replay and the P2-6 open-session/check-out-anchor replay, before any attendance processing, detector load, or camera open; the durable engine's session hook is the journal-backed lookup. Corrupt/unreadable journals stop durable startup before any hardware is touched; default non-durable behavior is unchanged and `--durable` remains opt-in.
* `tools/attendance_report.py --summary`: `CHECK_INS` counts `check_in` events only (pre-P2-6 journals keep the prior output byte-identically) and an additive `CHECK_OUTS` table counts `check_out` events only when the selection contains at least one checkout; count-oriented only (no duration/payroll inference).
* Documentation: README journal-compatibility/upgrade-rollback section, ARCHITECTURE D23, DEVELOPMENT_STATUS Step 26.

### Notes

* **Rollback compatibility risk (verified from source):** releases through the P2-5 checkpoint (commit `38984f6`) map the unknown `check_out` event type to `CorruptAttendanceJournalError` during `load_events()` — a whole-journal fail-closed rejection, not a per-record skip. A journal that already contains checkout events becomes entirely unreadable by such releases; the recommended practice is a verified journal backup before upgrading and a backup restore (never hand-edited journal lines) for rollback. The store's unknown-event-type and torn-tail handling is otherwise unchanged.
* Ordinary verified attendance continues to produce only `check_in` events; a checkout only occurs through an explicit checkout decision request reaching the engine. Test verdicts (2026-10-27 session, hardware-free, synthetic data): recovery 56, core battery 258, integration/durable/report 104, integration-demo CLI 49, report CLI 40 — all passed, exit 0 each; `git diff --check` exit 0. No camera, no real biometric/attendance data, no Git mutations.

## [0.24.0] — 2026-10-24 — Phase 2, P2-5: Opt-in durable composition wired into live and smoke modes (owner-approved wiring milestone)

### Added

* `tools/run_integration_demo.py`: one opt-in `--durable` flag shared by `--smoke` and `--live` (default **off**; default-off invocations keep the exact pre-P2-5 behavior and output). Smoke mode propagates the flag into `run_smoke` and, when enabled, prints a clearly-labeled durable summary (cooldown anchors recovered, the four persistence-outcome counts, and a note that the run's journal lives in the temporary directory removed on exit). A durable smoke run stopped by a journal-load or recovery error exits `1` with a clear `durable smoke mode stopped before any event processing` message — fail closed, never silently degraded. Live mode passes the flag through `LiveModeConfig.durable`; a journal-load or recovery failure is reported through the existing live-failure path (nonzero exit) because it is raised before the camera ever opens.
* `src/integration_demo.py` — `run_smoke`: new keyword-only `durable=False` and `wall_clock=None`. When durable, the real Step 13 engine is composed with the P2-1 `AttendanceEventStore` via the unchanged P2-2 `DurableAttendanceService`, and the P2-3 `recover_cooldown_anchors` replay runs **before any frame is processed** (a journal-load or recovery error propagates — durable mode stops before processing events, never silently downgraded). The documented four outcome states are reported verbatim (nothing redefined); created events are appended exactly when the engine creates them (per-record fsync, uncertainty never retried); the synthetic but real-shaped fixture behavior is unchanged. `SmokeRunResult` gains additive default fields `durable_outcomes` (per-frame persistence statuses in frame order; empty when flag-off) and `durable_recovered_anchors`; the no-event honesty path (no fabricated events) is unchanged and now reachable in durable runs too. The smoke journal is scoped to the run's temporary `AppSettings` — the smoke temporary directory is removed with it on exit.
* `src/integration_demo.py` — `run_live`: `LiveModeConfig` gains an additive `durable=False` attribute (default path untouched: no store constructed, no journal directory read or written, recovery never called). With the flag on, the durable engine/store/recovery/service composition runs **before** any hardware acquisition — recovery completion precedes the detector load and camera open, and journal-load or recovery errors stop the run before anything is opened (fail closed). Verified frames carry the same `VERIFIED …` line as before and additionally print one `durable:` line for `PERSISTED` / `PERSISTENCE_REJECTED` / `PERSISTENCE_UNCONFIRMED` outcomes (`NO_EVENT` stays silent, matching the unknown-face reporting convention); uncertain appends are never retried; unknown-face handling, recognition thresholds, temporal policy, and event contracts are exactly as before. `run_live` gained a keyword-only `wall_clock` injectable (default `datetime.now(UTC)`) solely for the durable branch's engine timestamps and recovery age computation.
* `tests/test_durable_live_mode.py` (16 tests) plus 13 new tests in `tests/test_run_integration_demo_cli.py`: flag parsing/default-off/position-independence and exact propagation into `run_smoke` (both flag states) and `LiveModeConfig` (with the other live options provably untouched); durable smoke happy path (exact one-event persistence, `("no_event","no_event","persisted","no_event","no_event")` outcome sequence, wall-clock timestamp agreement, durable detail segments, five-key journal privacy envelope); restart simulation (journal carried to a fresh run → recovery restores the anchor → frame-3 suppression, nothing re-appended); cooldown boundary (journaled age 299 s suppressed; exact 300 s expired → fresh event persisted); persistence rejected/uncommitted surfacing with exactly one append attempt (never retried) and a provably unchanged/empty journal; corrupt journal and unsupported schema version stopping durable smoke typed-ly before processing; flag-off smoke never creating the journal directory and never calling recovery (probe-tested); durable live fail-closed ordering proven with recording camera/detector doubles (corrupt journal → error before any hardware load), durable live clean run printing the recovery summary and writing nothing, and flag-off live leaving the journal untouched; durable smoke CLI failure exit 1 with the typed error surfaced.

### Notes

* **Default-off byte discipline (tested):** without `--durable` no journal directory/file is created anywhere, `recover_cooldown_anchors` is never invoked, and smoke/CLI output is exactly the pre-P2-5 output; the durable summary and `durable:` details appear only in flag-on runs. Durable smoke journals inside its temporary directory (removed with it); durable live journals to the configured `data/attendance/events.jsonl` (UTC timestamps only, unchanged schema). Failure handling: recovery/journal-load errors fail closed in both modes (smoke CLI exit 1 with typed message; live raises through the existing failure path before the camera opens); uncertain appends are reported and never automatically retried (the P2-2 contract).
* Phase 2 wiring honesty: P2-1 (store), P2-2 (composition), P2-3 (recovery), and P2-4 (report CLI) existed unchanged and were wired by P2-5 into opt-in live/smoke paths only — the report CLI is not part of any run loop (it remains an on-demand operator tool). No thresholds changed; no unknown-face biometrics are stored (the unknown-face path is untouched); no frames, crops, embeddings, or similarity scores are logged; no check-out/shift/lateness/daily-dedup business rules were added (separate milestone, sequence unchanged). **Real-camera durable behavior is not verified**: the owner may separately run `--live --durable` and record the outcome — no such evidence exists yet. Empirical CV accuracy evaluation remains BLOCKED pending the required consented labeled dataset and evaluation procedure. Focused suites 65 passed (13 new CLI + 16 new durable-mode + previous CLI tests) and related suites 260 passed; full suite 994 passed (965 prior + 29 new), exit 0 each (`pipefail`-captured); camera not opened during any automated test; no real biometric/enrollment/attendance data accessed; nothing staged or committed (Git state untouched).

## [0.23.0] — 2026-10-24 — Phase 2, P2-4: Read-only attendance report CLI (operator inspection only, no live wiring)

### Added

* `tools/attendance_report.py`: a read-only operator CLI over the unchanged P2-1 journal (`<DATA_DIR>/attendance/events.jsonl`). Lists events in deterministic chronological order by default (`occurred_at`, ties resolved by original journal position — the project's documented tie-break) with `--journal-order` as the explicit append-order alternative; verbatim repeatable `--employee` ID filtering (IDs are never normalized); UTC date filtering as half-open intervals (`--since` inclusive at that date's midnight; `--until` inclusive over its whole day via a next-midnight exclusive bound — fractional seconds fully covered; `--day` = both together, mutually exclusive with the pair); `--summary` per-employee × per-day counts (day order, then verbatim employee ID). Duplicate `event_id` records are displayed as-is, never de-duplicated or warned about (the store's documented limitation is reported, not hidden). Output carries exactly the four schema fields (UTC timestamp, event type, employee ID verbatim, event ID verbatim) — no scores, names, or biometric content.
* Deliberate exit contract, argparse's default error exit overridden: `0` success (including an empty journal and an incomplete trailing record — the torn tail is surfaced as a stderr warning while valid preceding rows print, per the loader's exclude-the-fragment semantics, never repaired); `2` corrupt journal / unsupported schema version / unreadable data — the store's typed error goes to stderr and NO event rows are printed (a failed load is never presented as an empty report); `3` invalid arguments or dates — checked before any journal access; `--help` follows the untouched default argparse path (prints help, 0).
* Import/execution hygiene (asserted by tests): importing the tool module loads no `src` modules at all (the store imports are deferred into `main()`, because the store's module graph transitively includes recognition-layer types); executing a full report imports the P2-1 store but never constructs a camera, detector, or recognizer model (proven by turning `cv2.VideoCapture` / `cv2.FaceDetectorYN` / `cv2.FaceRecognizerSF` / `onnxruntime.InferenceSession` into hard failures in a test); importing creates no data-directory tree.
* `tests/test_attendance_report_cli.py` (36 tests): empty/missing/zero-byte journals exit 0; single and multi-event listings; chronological default with a journal-position tie-break; `--journal-order` distinct ordering; verbatim/repeatable employee filtering with no-match handling; half-open date boundaries including fractional seconds at 23:59:59.999999Z and the next-midnight exclusive edge; deterministic `--summary` (row order and counts asserted, filter-respecting); torn-tail warning with rows; corrupt mid-file and unsupported-schema → exit 2 with no rows and byte-identical journal; invalid dates/`--day` conflicts/empty interval → exit 3 with untouched journal bytes; `--help` → 0; journal bytes byte-identical after every success-path invocation; subprocess import hygiene (no `cv2`/camera/biometric modules in `sys.modules`, no data-dir creation); in-process no-initialization guard.

### Notes

* Explicit limitations: UTC-only date display and filtering (site-local timezone support deliberately deferred — the journal stores UTC only); duplicate `event_id` lines print as-is by design. The CLI is **not wired into the live attendance workflow** (no caller; the live demo remains unconnected to persistence, unchanged); it never appends, truncates, repairs, rewrites, or locks the journal. Journal schema, store behavior, recovery API, and the P2-2 four-state outcome contract are unchanged. No new dependencies (stdlib `argparse`/`datetime` only); no files under `src/`, `requirements.txt`, or `pytest.ini` touched. Focused CLI suite 36 passed and full suite 965 passed (929 prior + 36 new), exit 0 each under `pipefail`; camera not opened; no real biometric/enrollment/attendance data accessed; nothing staged or committed.

## [0.22.0] — 2026-10-23 — Phase 2, P2-3: Restart recovery of attendance cooldown anchors (read-only replay, no live wiring)

### Added

* `src/attendance_recovery.py`: `recover_cooldown_anchors(engine, store, *, wall_now)` — deterministic, read-only restart recovery. Loads the journal in full through the unchanged P2-1 `load_events()` **before** any engine mutation (a failed load can never leave a partially recovered engine; `CorruptAttendanceJournalError` / `UnsupportedJournalSchemaVersionError` propagate unchanged — a failed load is never treated as an empty journal), selects per employee the latest persisted `CHECK_IN` by UTC `occurred_at` (ties by journal position; deterministic under out-of-order records and the store's documented duplicate-`event_id` retry limitation), and restores each unexpired anchor via the engine's new additive API. Nothing is appended, rewritten, or created; no attendance event is emitted; the torn-tail `incomplete_trailing_record` flag is passed through on the frozen `CooldownRecoveryReport` (restored IDs, expired/clamped counts, records scanned). Log lines carry counts only — never employee IDs, never biometric content.
* `src/attendance.py` (additive only): `AttendanceEngine.restore_suppression_anchor(employee_id, *, elapsed_seconds)` — places one anchor at `monotonic_now - elapsed_seconds` in the current run's monotonic domain; returns `False` when the age is already at or beyond the configured cooldown (expired — nothing stored). The engine remains the single cooldown authority (its own `check_in_cooldown_seconds` decides expiry; LRU bounded-state policy identical to `update()`); a wall timestamp is never stored as or compared as a monotonic value. Existing engine behavior is untouched (the monotonic read/validation was factored into `_monotonic_now()` verbatim).
* `tests/test_attendance_recovery.py` (28 tests) and a new `TestRestoreSuppressionAnchor` class in `tests/test_attendance_engine.py` (16 tests): empty/missing journal recovery with zero side effects, one-event restore with exact remaining-cooldown boundary, per-employee independence, latest-event selection, real-chain replay (engine + store + `DurableAttendanceService` over a temp journal — suppressed frames and non-verified results never become replayable events), expiry at/after the exact boundary, out-of-order and duplicate-ID determinism, torn-tail exclusion with flag pass-through, corrupt/unsupported-schema all-or-nothing failure (no false success, no partial state), journal bytes byte-identical after replay, the explicit clock-skew policy (backward → clamp to full cooldown; forward → expired worst case one extra event), wall↔monotonic mapping, and replay-then-process matching pre-restart behavior.

### Notes

* Clock-skew policy (explicit, conservative, documented in D20 and the module): wall clock moved backward across a restart → negative age clamped to zero (suppress for the full cooldown — worst case one *delayed* legitimate check-in, never a duplicate); wall clock moved forward → expired anchors are simply not restored (worst case one additional check-in, the engine's documented bounded-state worst case). Honest limit: cross-run cooldown precision is bounded by wall-clock reliability between runs; within a run, suppression remains monotonic-exact. Recovery is not wired into live mode (no caller); the P2-2 four-state outcome contract and the README live-mode statement are unchanged. No engine decision semantics, journal schema, threshold, or dependency changed. Focused suite 96 passed (28 recovery + 16 new engine tests plus the two modules' existing tests; engine/store/persistence re-runs green), relevant suites 212 passed, full suite 929 passed (exit 0 each).

## [0.21.0] — 2026-10-23 — Phase 2, P2-2: Durable attendance composition (engine + store, no live wiring)

### Added

* `src/attendance_persistence.py`: `DurableAttendanceService` — composes the unchanged Step 13 `AttendanceEngine` (decision authority) with the unchanged P2-1 `AttendanceEventStore` (persistence authority). `process(result)` feeds the temporal result to the engine unmodified; a decision without an event appends nothing; a created event is appended exactly once. A frozen `DurableAttendanceOutcome` reports exactly four explicit states: `NO_EVENT` (verbatim decision carried), `PERSISTED` (append/fsync confirmed), `PERSISTENCE_REJECTED` (the store raised `AttendanceStoreInputError` before any write — a confirmed failure: journal provably unchanged, record definitively absent), and `PERSISTENCE_UNCONFIRMED` (any other store error — an uncertain outcome; the original exception object is preserved unmodified with the exact event and a short reason — never presented as a durable check-in, and **never automatically retried**, since the journal may already contain the record and a retry could duplicate it). Other store exceptions and engine `AttendanceInputError`s propagate unchanged. Collaborators are structurally validated (callable `update`/`append`), so injected doubles work; construction performs no filesystem work; log lines carry statuses only, never employee IDs.
* `tests/test_attendance_persistence.py`: 19 deterministic, hardware-free tests — the three-outcome matrix (no-event ×3 statuses with zero appends and verbatim decision carriage; persisted exactly-once same-object append; unconfirmed with identity-preserved exception), no automatic retry across repeated failures, store input-error family mapping, non-store programming errors propagating unlabeled, engine exception propagation with zero appends, frozen outcome, construction validation and no-side-effects, and real-engine integration with injected clocks (VERIFIED → one exact event; same-instant suppression → no append; exact 300 s cooldown boundary → second event).

### Notes

* No attendance decision, store contract, schema, threshold, or dependency changed; the live demo, smoke mode, and CLI tools are untouched — the service has no caller yet and wiring it into live mode is explicitly future work. Engine cooldown anchors remain engine-instance-lifetime; restart recovery is future work. Focused suite 19 passed, relevant suites 166 passed, full suite 883 passed (exit 0 each).

## [0.20.0] — 2026-10-22 — Phase 2, P2-1: Durable attendance event store (storage only)

### Added

* `src/attendance_store.py`: `AttendanceEventStore` — an append-only JSON Lines journal persisting existing, already-validated `AttendanceEvent` values at `<DATA_DIR>/attendance/events.jsonl` (path derived from `AppSettings` like every other storage root). On-disk record schema is exactly five keys (`schema_version: 1`, `event_id`, `employee_id`, `event_type`, `occurred_at` in UTC ISO-8601); the same five-key envelope is enforced on load, so records carrying extra keys — biometric, diagnostic, or future — are typed errors, never silently reinterpreted. No face images, crops, embeddings, similarity scores, or unknown-person data pass through this module.
* Torn-tail recovery contract: after an interrupted append, loading excludes an incomplete trailing fragment and reports it (`incomplete_trailing_record=True`) instead of hiding it; corruption anywhere else raises `CorruptAttendanceJournalError` with the exact journal line number and is never silently skipped. Appending over a torn tail repairs it first — a trailing fragment that is a fully valid record is salvaged by appending the missing newline (the record is preserved); otherwise the complete-line prefix is republished via the staged-temp-file → fsync → atomic `os.replace` pattern (the Step 09 precedent), the store's only rewrite path. A failed repair raises and leaves the journal byte-unchanged.
* Append durability policy: normal appends never rewrite the journal — `open("a")` + `flush()` + `os.fsync()` per record; a record counts as persisted only after fsync returns, and every directory/write/flush/fsync failure raises `AttendanceStorePersistenceError` with the caller told to treat the event as unconfirmed (documented, and pinned by a test, that a record may still physically be present after an fsync failure — appends cannot be retracted).
* `src/config.py`: additive derived path `AppSettings.attendance_dir` (`data_dir / "attendance"`) mirroring `embeddings_dir`; no existing configuration, validation, or behavior changed.
* `tests/test_attendance_store.py`: 47 deterministic, hardware-free tests (append/loading/order/per-type round-trips, verbatim IDs and timestamps, load-is-read-only, no-construction side effects, privacy envelope, full load-validation matrix with line numbers, unsupported schema versions, torn-tail salvage/truncate/failed-repair semantics, typed error hierarchy).

### Notes

* Storage only in this step: the store is not wired into the live demo or the attendance engine yet (next phase step), and no existing attendance behavior changed — the full suite passes unchanged (858 tests = 811 prior + 47 new). Honest limits recorded: no cross-process file locking or `event_id` dedup (retry/once-policy belongs to the composition layer), fsync covers the file — not the directory entry — and there is no query/audit API yet; empirical CV evaluation remains separately blocked and untouched.
* Post-audit fix (same day): an ID carrying a lone Unicode surrogate passes event construction but cannot be UTF-8 encoded — `append()` now raises the documented typed `AttendanceStoreInputError` (cause preserved, journal verified byte-identical, no partial data) instead of an unwrapped `UnicodeEncodeError`; four new boundary tests pin surrogate rejection, zero-byte-journal loading, directory-at-journal-path behavior in both directions, and load of a complete valid trailing fragment missing only its newline (full suite now 864 tests).

## [0.19.0] — 2026-10-22 — Phase 1, Step 20: Unknown-face handling wired into live mode (report-only, real handler)

### Added

* `src/integration_demo.py` (`run_live`): the live pipeline now integrates the existing `UnknownFaceHandler` (Step 12) through its authoritative `observe_temporal` contract — the same pattern smoke mode already uses. The real handler is constructed once per live session (default frozen `UnknownFaceConfig` with the bounded 30 s non-refreshing cooldown and `max_sessions` LRU cap; in-memory only, no persistence). On every full-chain frame (single face, quality-accepted, recognizer available) the frame's temporal verification result is fed to the handler, and live console output gains deterministic report-only lines: `unknown-face outcome observed (…)` for the first unresolved observation of an episode, `suppressed (…; suppressed_frames=N)` for repeats inside the cooldown, and `episode_ended (suppressed_frames=N)` when a temporally verified identity resolves the episode. `NO_ACTION` (nothing unknown, or a verified identity with no active episode) stays silent. Frames without detection/quality rejection never fabricate temporal evidence (the live loop never ran the recognizer on them before and still does not); single MATCHED frames remain non-verified per the Step 11 streak contract; recognition, temporal verification, attendance decisions, duplicate suppression, and camera cleanup are unchanged.
* `run_live` gained a keyword-only `monotonic` injectable clock (same default-resolution convention as `run_smoke`; default `time.monotonic`) solely so the episode-cooldown boundaries stay deterministic in hardware-free tests.
* `tests/test_integration_demo.py`: 9 focused hardware-free tests — pure `unknown-face outcome` formatting (observed/suppressed/episode-ended, count appendix, no-appendix cases); the report helper against the real `UnknownFaceHandler` with a deterministic clock (first unresolved observation reported once, repeats suppressed with the running count, a verified identity resolving the episode with EPISODE_ENDED and without leaking the employee ID into the unknown-face line, and NO_ACTION silence); and a full live-loop wiring test with injected camera/detector/assessor/embedder/recognizer/verifier/attendance doubles asserting the exact OBSERVED → SUPPRESSED → SUPPRESSED → EPISODE_ENDED → fresh OBSERVED console sequence, exactly one attendance decision (verified frame only), and clean camera release.

### Notes

* Report-only: the unknown-face lines print statuses and suppressed-frame counts only — never crops, embeddings, identities, or other quality metrics, and nothing is written to files or logs by this wiring. The live demo's attendance semantics are untouched: an unresolved observation can never authorize an event.

## [0.18.0] — 2026-10-20 — Phase 1, Step 18: Operator deletion CLI (preview → two-stage confirmation → derived-path scoped removal → verified absence)

### Added

* `tools/delete_enrollment.py`: operator CLI removing exactly one employee's enrollment artifacts (stored embedding `.npz` + `.meta.json`, the `employees_dir` manifest directory published by Step 07, and the `face_samples` directory). No deletion API exists in the existing services, so removal is strictly *derived-path* file/directory deletion built on the public validation APIs: the ID is validated through `src.enrollment.safe_dir_name` (exact Employee rules; traversal/`..`/dot-forms rejected before any filesystem work), every removed path is derived from the configured `AppSettings` roots only, and defensive containment checks re-verify each derived parent against the resolved root. Preview (plain run) prints present/missing status per artifact group with zero changes (exit 0); `--execute` without `--confirm-delete` is refused (exit 2, nothing runs); both separate flags together trigger deletion and never bypass ID validation or the target-existence check (a nonexistent enrollment is refused, exit 1). Removal order mirrors enrollment's publish order in reverse (embedding record → manifest → samples), so a mid-deletion crash can only leave the publish-safe orphaned-samples state, never a declared enrollment without images. Failures report exactly what was removed and what remains (exit 1, never fabricated success); success requires the ID to be provably absent through the real `EmbeddingStorageService.list_employee_ids()` plus direct artifact-existence checks. File contents (crops/vectors) are never read, printed, or logged. Exit codes: 0 success, 1 typed failure, 2 confirmation/argparse refusal.
* `tests/test_delete_enrollment.py`: 10 focused categories (all tmp-dir + injected double): invalid/malicious ID rejection before any filesystem work; nonexistent enrollment refusal with zero changes; preview makes no changes; missing individual artifacts tolerated; `--execute` without `--confirm-delete` refused; successful deletion of all artifact groups; deletion/storage failure with partial-failure reporting; final candidate-verification failure (ID still listed → no success); isolation (another employee's files and shared model files untouched); success only when the target is no longer discoverable.

### Notes

* Deletion is irreversible: recovery is possible only by re-enrolling a re-consenting participant (documented in README). The live demo remains never-enrolling and never-deleting; both operations stay operator-driven. No thresholds, model files, dimensions, or service contracts were changed.

## [0.17.0] — 2026-10-20 — Phase 1, Step 17: Operator enrollment CLI (consent-gated enroll → bridge → candidate verification)

### Added

* `tools/enroll_employee.py`: operator CLI connecting the existing Step 07 `EnrollmentService.enroll` to the existing Step 16 `embed_published_samples` bridge — one command makes a consenting employee a recognition candidate. Composition only: no new recognition math, thresholds, persistence, camera, or model behavior. Flow: mandatory `--confirm-consent` gate (without it the tool does literally nothing — no prerequisite probe, no camera, no validation — and exits 2) → prerequisite probing for the operator-supplied `models/yunet.onnx` / `models/sface.onnx` (clear abort before any capture) → the existing bounded enrollment session (its own duplicate refusal and typed errors surface unchanged) → the bridge for exactly one embedding under `replace=False` → final proof that the ID is discoverable via `storage.list_employee_ids()` (the live recognizer's candidate path) before printing success. Failure reporting is honest and never fabricates success: duplicate enrollment/existing embedding → refused, nothing changed; embedding failure after a completed capture → the published enrollment stays on disk (legitimate Step 07 data) and the recovery path is printed. No crops, vectors, or embeddings are ever printed or logged. Exit codes: 0 success, 1 typed failure, 2 argparse/consent refusal.
* `tests/test_enroll_employee_cli.py`: 8 focused CLI categories (all mocked, hardware-free, biometric-free): consent declined does nothing; consent accepted runs enroll → bridge in order; real-validation ID/name rejection with the camera factory never touched; duplicate enrollment and duplicate existing embedding refusal (nothing overwritten); capture/enrollment failure (no embedding work); embedding generation/storage failure; final candidate-verification failure (no success message); success only when the ID appears in `list_employee_ids()`. Extended 2026-10-21 with the frame-size categories (parser surface, camera-config composition, flow/main wiring), still hardware-free.
* Opt-in live-demo sharpness diagnostic (2026-10-21): `tools/run_integration_demo.py --live --report-quality-metrics` — an explicit, disabled-by-default console-only flag under which frames the per-face gate rejects **specifically for blur** additionally print the already-measured sharpness value and the configured threshold, e.g. `[frame   12] rejected by quality: blur (sharpness=18.3, threshold=50.0)`. The metric is never recomputed, never saved to files, and never appended to `data/logs`; with the flag absent the output is byte-identical to the pre-diagnostic format. Motivated by the measured blur-rejection pattern on the development camera whose actual numeric values were computed but never surfaced; the values enable evidence-based Phase-2 threshold-calibration discussion and never auto-tune anything. Implementation: config flag on `LiveModeConfig`, a pure `_format_quality_rejection` formatter (plain line when the sharpness metric is unmeasured — never an invented value), CLI wiring that injects the keyword only when the operator passes the flag.
* Failure-output diagnostic tally on `tools/enroll_employee.py` (2026-10-21): the enrollment-failure line now appends a deterministic `rejections by reason` summary — counts of the already-recorded `EnrollmentResult.rejections` by their stable `FaceQualityReason` value, reasons lexicographically sorted (e.g. `rejections by reason: blur=1, no_faces=2, too_small=1`); omitted entirely when the session recorded no rejections. Diagnostic-only: prints reason names and counts, never frame indices, crops, quality metrics, or any biometric data; capture logic, acceptance thresholds, camera configuration, persistence, and success behavior are all unchanged. Motivated by the measured 60-attempt/0-sample failure whose specific rejection split was recorded in the result but hidden from the operator output.
* `--frame-size WIDTHxHEIGHT` on `tools/enroll_employee.py` (2026-10-21): optional camera frame-size override for the enrollment capture session — the same validated parser as `tools/run_integration_demo.py --frame-size`, reused rather than duplicated. Motivated by a measured failure on the development machine: the index-0 camera reports 640×480 while the configured default is 1280×720, so under the default the capture session aborted in `CameraService.open()` before any frame ("frames attempted: 0"; nothing published). The override changes only the *requested* frame size; the device index, all camera validation (`CameraOpenError` when the device cannot apply the requested size), and the configured default resolution remain unchanged.

### Notes

* The live demo (`tools/run_integration_demo.py --live`) remains never-enrolling; enrollment stays operator-driven and consent-gated. Real-camera recognition accuracy is still not claimed: automated tests exercise wiring with injected doubles only, and a live consented participant run remains the sole proof of real recognition.

## [0.16.0] — 2026-10-19 — Phase 1: Enrollment-to-recognition bridge (sample PNGs → one stored embedding per employee)

### Added

* `src/enrollment_bridge.py`: `embed_published_samples(employee_id, *, embedder, storage, settings)` — connects Step 07 enrollment to Step 10 recognition without duplicating any service: requires the published Step 07 `employee.json` eligibility marker (schema version 1, exact ID match, bool `is_active`), decodes every published face-sample PNG fail-closed (one unreadable sample aborts before any write), embeds each via the existing `FaceEmbedder` (typed errors surface unchanged), aggregates as the cosine centroid (per-sample L2 normalization then unit mean — verified compatible with the raw-feature cosine decision metric and the storage norm contract), and persists exactly one embedding via `EmbeddingStorageService.save(..., replace=False)` (duplicate records abort before any embedding work; no force-replacement path). Success re-proves recognition-side consistency through `storage.load` and `storage.list_employee_ids()`. No vectors/crops are ever logged or returned; the result carries facts only (ID, dimension, sample count).
* `tests/test_enrollment_bridge.py`: 16 focused tests (all synthetic non-biometric inputs, faked embedders, tmp dirs): successful bridge + candidate discovery through the real `FaceRecognitionService` (MATCHED) and `list_employee_ids`; exact cosine-centroid aggregation; missing/contradictory/incompatible manifests; zero samples and unreadable samples (fail-closed); ID-form rejection; non-storage instance rejection; embedder/dimension/zero-norm/canceling-sample failures persisting nothing; duplicate protection both via pre-existing records and second bridge runs; storage persistence failure.

### Notes

* No thresholds, model files, embedding dimensions, storage contracts, or existing services were changed. End-to-end *live* recognition after an enrollment still requires an authorized operator enrollment with a consenting human participant (real camera, real models); unit tests use synthetic vectors only and do not claim real-model recognition accuracy.

## [0.15.0] — 2026-10-14 — Phase 1, Step 15: Integration demo (integration & smoke green; live-camera real-world results not obtained; empirical CV evaluation still blocked)

### Added

* `src/integration_demo.py`: the Phase 1 integration orchestrator —
  composes the existing approved components (Steps 04–13) end to end
  (capture → detect → quality → embed → recognize → temporal verify →
  unknown-face handling → attendance event) with **no** new pipeline
  math or thresholds. Two explicitly separated modes: `run_smoke`
  (deterministic, hardware-free, temp-dir-only fixtures; real
  downstream engines; honest no-event outcomes) and `run_live`
  (explicitly opt-in real-camera run with prerequisite probing, a
  clearly-labeled partial demo while `models/sface.onnx` is absent,
  and guaranteed resource release on every exit path).
* `tools/run_integration_demo.py`: CLI entry point (`--smoke` default;
  `--live` opt-in with `--camera-index`, `--no-window`,
  `--max-frames`; optional `--frame-size WIDTHxHEIGHT` camera-frame
  override for `--live`, validated as two positive integer dimensions
  with a clear CLI error otherwise). Verified via `--help` and a
  `--smoke` run.
* `tests/test_integration_demo.py`: 20 focused tests covering the
  single-event happy path (exact ID `"007"`), per-stage checkpoint
  coverage, boundary-injection integrity, no-event guarantees for
  quality-rejected/no-face/unknown-evidence runs, real-data safety
  (repository `data/` untouched before/after), typed input errors,
  and the live mode's abort/prerequisite/headless paths.

### Notes

* Only a real, temporally `VERIFIED` identity reaches the real
  attendance engine; the integration layer never fabricates events
  or recognition results. Real camera/SFace inference was not
  exercised at Step 15 recording time (`sface.onnx` was then absent);
  empirical CV evaluation remains blocked exactly as recorded for
  Step 14.

## [0.14.0] — 2026-10-13 — Phase 1, Step 14: Testing & evaluation (software regression green; empirical CV evaluation blocked)

### Added

* `notebooks/09_phase1_evaluation.ipynb`: reproducible, privacy-safe
  Phase 1 evaluation notebook — environment & artifact availability
  (yuNet `yunet.onnx` PRESENT, `sface.onnx` MISSING; nothing is ever
  downloaded), safe regression-suite instructions with an opt-in
  runner cell, software-level detection contract checks + documented
  IoU helper + explicitly opt-in local YuNet smoke check on a
  synthetic frame, synthetic-quality threshold rows and face-count
  policy, synthetic-identity recognition decision rows,
  temporal/unknown-face/attendance behavior rows with injected fake
  clocks (no sleeps), machine-dependent software-overhead timing and
  an explicit limitations/spoof-boundary subsection. No camera is
  opened, nothing enrolled, the production `data/` store untouched,
  biometric files never read.
* `reports/phase1_evaluation.md`: evidence-based evaluation report;
  every value labeled MEASURED / TEST COVERAGE / NOT RUN / BLOCKED /
  NOT MEASURED with reasons — regression results, artifact table,
  detection (precision/recall NOT RUN — no annotated dataset),
  synthetic quality rows, recognition decisions with FAR/FRR NOT
  MEASURED (missing embedding model), behavior rows for temporal/
  unknown-face/attendance incl. exact cooldown boundaries, the
  documented-but-unexecuted spoof scenario plan (no resistance
  claim), risks, and reproduction instructions.

### Notes

* Step 14 status is **not** claimed fully passed: software regression
  (654 tests) is green; empirical CV evaluation (accuracy, FAR/FRR,
  real-lighting quality, spoof resistance, camera latency) remains
  blocked on the missing `sface.onnx` artifact and consented labeled
  data. No model, dataset, or biometric files were added.

## [0.13.0] — 2026-10-13 — Phase 1, Step 13: Attendance event engine

### Added

* `src/attendance.py`: `AttendanceEngine` — deterministic,
  independently testable attendance-event engine strictly on the
  approved Step 11 trust boundary: only a structurally valid
  `VERIFIED` temporal result can authorize a check-in; raw
  recognition results (including `MATCHED`) are rejected input;
  `COLLECTING`/`INTERRUPTED` yield `NOT_VERIFIED`; malformed inputs
  and invalid clock outputs fail safely with typed
  `AttendanceInputError` (no event, no state change, no fabricated
  success). Optional injectable `active_lookup` (the Step 10 hook
  pattern; no new manifest/storage reads) yields
  `INACTIVE_EMPLOYEE` for inactive verified employees and is never
  invented when absent.
* Structured event & decision: frozen `AttendanceEvent(event_id,
  employee_id, event_type, occurred_at)` — `EventType.CHECK_IN` only
  (no check-out/shift/lateness/payroll logic in this step);
  timezone-aware `occurred_at` normalized to UTC (naive rejected);
  documented `uuid.uuid4().hex` event-ID default with validated
  injectable factory; no biometric data carried. Frozen
  `AttendanceDecision(status, event, reason)` with
  `{EVENT_CREATED, NOT_VERIFIED, INACTIVE_EMPLOYEE, SUPPRESSED}`
  — `event` is non-`None` only on `EVENT_CREATED`.
* Duplicate suppression (in-memory, bounded, non-refreshing): one
  `CHECK_IN` per employee per `check_in_cooldown_seconds=300.0`
  (initial engineering value, not a calibrated policy); exact
  boundary (`elapsed < cooldown` suppressed, `== cooldown` new
  event); anchors on an injectable monotonic clock — never refreshed
  by repeated frames; per-employee independence; LRU-bounded at
  `max_tracked_employees=64`. **Documented restart limitation:**
  anchors live for the engine instance only — this is not durable,
  cross-process exactly-once protection.
* Explicit clock separation: `clock_wall` (timezone-aware UTC
  datetimes, naive rejected) for event timestamps vs
  `monotonic_clock` (finite values, invalid outputs rejected) for
  suppression — backward wall-clock movement cannot bypass duplicate
  protection or expire anchors. No sleeps in tests (injected fake
  clocks).
* `tests/test_attendance_engine.py`: 52 deterministic,
  hardware-free tests covering the 30-item Step 13 checklist (trust
  boundary, event schema, suppression boundaries and expiry, config
  validation, clock failures and backward time, reset semantics,
  instance isolation, factory-failure atomicity, bounded LRU state,
  and side-effect-free import/construction).

### Notes

* The engine is not connected to the camera loop; no orchestration,
  dashboard, database/CSV persistence, or audit trail was added.
  Identity/recognition/attendance responsibilities remain separate.
  Step 14 (testing & evaluation) and Step 15 (integration demo) were
  not started.

## [0.12.0] — 2026-10-13 — Phase 1, Step 12: Unknown-face handling

### Added

* `src/unknown_face_handler.py`: `UnknownFaceHandler` — conservative,
  deterministic, in-memory unknown-face handling strictly on top of
  the approved Step 10 recognition and Step 11 temporal-verification
  contracts. Two documented entry points: `observe_temporal()` is the
  authoritative trust boundary (only a `VERIFIED` temporal result
  resolves an unresolved episode as a known, temporally verified
  presence; outcomes never carry an identity), `observe_recognition()`
  is observability-only (a raw `MATCHED` is classified
  `MATCH_UNVERIFIED` — never an identity). Frozen
  `UnknownFaceOutcome(status, observation, suppressed_frames, reason)`
  with `UnknownFaceStatus.{NO_ACTION, OBSERVED, SUPPRESSED,
  EPISODE_ENDED}` and one-to-one `UnresolvedObservation` values; no
  employee IDs, scores, frames, crops, or names ever carried.
* Deterministic episode/cooldown policy: first unresolved observation
  opens one `OBSERVED` alert per caller-managed session; repeats
  within `episode_cooldown_seconds` are `SUPPRESSED` (counted); the
  cooldown is non-refreshing and the episode expires exactly at
  alert + cooldown (injectable monotonic clock — no sleeps in
  tests); a VERIFIED identity ends an active episode
  (`EPISODE_ENDED`); `reset(session_key=None)` ends one or all
  episodes explicitly.
* Bounded state: at most `max_sessions` episode records (LRU
  eviction), no unbounded histories; the project has no stable
  face-track identifier, so suppression is a documented
  unresolved-session policy, not evidence that repeated frames show
  one unknown person (no tracker added; no cross-person merging).
* `src/config.py`: frozen `UnknownFaceConfig` + `UNKNOWN_FACE` —
  `episode_cooldown_seconds=30.0`, `max_sessions=8`,
  `max_session_key_length=128`, all **initial engineering defaults**
  pending Phase 2 calibration; strict validation (bools rejected for
  numeric fields, NaN/inf/non-positive cooldowns rejected).
* `tests/test_unknown_face_handler.py`: 56 deterministic, hardware-
  free tests (synthetic results, injected fake clock) covering the
  verified/unverified boundary for every status, identity-free
  classification, cooldown boundaries and non-refreshing expiry,
  reset/EPISODE_ENDED semantics, bounded state and LRU eviction,
  session isolation and key validation, fail-safe malformed inputs,
  frozen types, and side-effect-free import/construction.

### Notes

* Unknown-face handling does **not** record attendance, generate
  events, persist any data (images/embeddings/audit trails), or add
  tracking/notifications/network. Real-camera integration and
  operational calibration remain future work.

## [0.11.0] — 2026-10-13 — Phase 1, Step 11: Temporal / multi-frame verification

### Added

* `src/temporal_verification.py`: `TemporalVerifier` — deterministic
  multi-frame temporal verification **on top of** the Step 10 result
  contract (consumes `RecognitionResult` values one at a time; no
  duplication of cosine similarity, thresholds, storage, or manifest
  logic). Evidence policy: only consecutive `MATCHED` frames for the
  **same** employee ID accumulate positive evidence; `UNKNOWN`,
  `AMBIGUOUS`, and `NO_CANDIDATES` never contribute and **break** the
  streak (`INTERRUPTED`, no identity reported); a matched frame for a
  different ID starts a fresh candidate streak (cross-ID evidence is
  never combined). Verification: `VERIFIED(employee_id)` exactly when
  the uninterrupted same-ID streak reaches `min_consistent_frames`
  (frozen `TemporalVerificationResult` with
  `COLLECTING`/`VERIFIED`/`INTERRUPTED` statuses, bounded
  `streak_frames`/`window_frames_used` counters, short reason).
  `reset()` clears all evidence (instance-scoped); one instance per
  tracked face/session — no shared state. Bounded retention only
  (deque capped at `window_frames`); no frames, crops, embeddings,
  scores, or names retained; employee IDs preserved verbatim
  (`"007"`, `"EMP-0042"`). Repeated post-verification frames are
  idempotent state reporting, not recurring verification events.
* `src/config.py`: frozen `TemporalVerificationConfig` +
  `TEMPORAL_VERIFICATION` — `min_consistent_frames=3`,
  `window_frames=10`, both **initial engineering defaults** pending
  Phase 2 real-camera calibration; strict validation (plain ints,
  bools/floats rejected; `window_frames >= min_consistent_frames`).
* `tests/test_temporal_verification.py`: 51 deterministic,
  hardware-free tests covering the temporal checklist (initial state,
  threshold, cross-ID isolation, ID preservation, per-status
  non-evidence, interruption, window bounds, reset & isolation,
  config validation, malformed `MATCHED` refusal, boundaries,
  bounded state, frozen results, and a side-effect-free
  import/construction subprocess check).

### Notes

* Temporal verification is **not** an attendance event; recording
  arrives in Step 13. Deterministic logic only — no real-camera or
  real-recognition-pipeline accuracy validation was performed.

## [0.10.0] — 2026-10-12 — Phase 1, Step 10: Face recognition

### Added

* `src/face_recognition.py`: `FaceRecognitionService` — one reusable,
  injectable recognition decision (crop → Step 08 embedder → Step 09
  storage candidates → deterministic cosine ranking → typed result).
  Decision only — no temporal verification (Step 11), no liveness, no
  unknown-face capture, no attendance events, no camera/writes/
  network. Recognition is a similarity-based decision, never proof of
  identity.
* Similarity & decision: pure cosine `a·b / (‖a‖‖b‖)` in [-1, 1]
  (`cosine_similarity`, unit-testable; validated 1-D/non-empty/
  finite/equal-dimension/non-zero-norm inputs; nothing mutated).
  Deterministic rank by `(-score, employee_id)`; statuses `MATCHED`/
  `UNKNOWN`/`AMBIGUOUS`/`NO_CANDIDATES` in frozen `RecognitionResult`
  (best/runner-up/margin scores + short reason; identity assigned
  only on unambiguous MATCHED). `best == threshold` ⇒ MATCHED;
  `margin == min_margin` ⇒ MATCHED; identical top scores ⇒ AMBIGUOUS
  (ties never resolve into identity). Single candidates decided on
  threshold alone.
* `src/config.py`: frozen `RecognitionConfig` + `RECOGNITION`
  — `min_similarity=0.36` (raw-cosine scale) and `min_margin=None`,
  both **explicitly provisional** until empirical calibration; range
  validation in `__post_init__`. The literature 0.6 threshold
  (normalized features) is deliberately not reused, because Step 08
  stores raw crop-only embeddings; `FaceProcessingConfig` unchanged.
* Employee eligibility via existing authorities only: the Step 07
  published enrollment manifest gates candidacy (no second source of
  truth); active status trusted from the manifest (or overridden by
  an injectable `active_lookup`); untrusted manifests are validated —
  unreadable/malformed/schema-mismatched/identity-contradicting ones
  raise the new typed `ManifestIntegrityError` (fail-closed default,
  documented `"skip"` escape hatch). Inactive employees are excluded
  from decisions but their records are never deleted. Stored IDs
  (`007`, `EMP-0042`) are preserved exactly.
* `src/enrollment.py`: backward-compatible public `safe_dir_name()`
  wrapper so the recognizer reuses the writer's exact encoded-name
  scheme (no duplication, no behavior change).
* Corruption & failure honesty: corrupt records surfaced via explicit
  candidates raise their typed Step 09 errors; a vanished record
  raises `RecognitionStorageError`; embedder failures raise
  `RecognitionModelError`; invalid inputs/config raise
  `RecognitionInputError`. Results/logs never contain crops or full
  vectors (privacy by contract, tested).
* `tests/test_face_recognition.py`: 74 deterministic tests on fake
  embedders + temp-dir real storage (no real SFace artifact, no
  camera, no network, no real biometrics) covering: matches, explicit
  subsets, unknown, exact threshold/margin boundaries, ambiguity,
  ties, candidate-set edges, vector validation matrix (NaN/inf/zero-
  norm/empty/dimension mismatch), corrupt-record propagation,
  embedder/storage failures, config validation, ID preservation,
  active/inactive + manifest integrity, model-compatibility honesty,
  immutability, privacy, import/construction/foreign-CWD/subprocess
  hygiene.

### Verified

* Focused recognition tests: 74 passed.
* `python -m pytest` → **495 passed** (421 prior + 74 new) from the
  project root and an external working directory; stable across
  repeated runs.
* `git diff --check` → clean; no biometric artifacts in the repo.

### Known limitations

* **Real SFace inference remains unverified** (artifact absent,
  never downloaded; Steps 08/09 status carried forward) — all
  recognition tests are fake-backed with real validation rigor, and
  no recognition accuracy claim is made.
* Model identity is not machine-enforced (no identifier exists in
  Step 09 sidecars and none was invented): all candidates in one
  deployment must come from one known, consistent embedding-model
  generation (documented operational remedy; no format change).
* Thresholds/margin are provisional starting points on the raw
  cosine scale; Phase 2 calibration required before production use.
* Not included per plan: temporal verification (Step 11), liveness,
  unknown-face capture, attendance events, camera loop.

## [0.9.0] — 2026-10-12 — Phase 1, Step 09: Embedding storage

### Added

* `src/embedding_storage.py`: `EmbeddingStorageService` — saves,
  loads, lists, and validates stored face embeddings under the
  existing configured `data/embeddings/` directory. Storage only: no
  matching (Step 10), no recognition, no attendance, no enrollment
  changes; storage proves nothing about identity.
* Storage format (one choice, documented): per employee one NumPy
  `.npz` array file (`np.savez` write / `np.load(allow_pickle=False)`
  read — no pickle, no object arrays) plus a JSON metadata sidecar
  (schema `format_version: 1`: exact employee ID, declared dimension,
  `float32` dtype marker, timezone-aware UTC `stored_at`). Record
  name stems reuse Step 07's traversal-proof percent-encoding; IDs
  (`007`, `EMP-0042`) survive byte-exactly and must satisfy the
  `Employee` model rules. No database, no new dependency.
* Contracts: `save(employee_id, embedding, *, replace=False)` accepts
  a Step 08 `FaceEmbedding` or a raw 1-D float array (normalized to
  the float32 on-disk contract); `load()` re-validates everything on
  every call treating stored files as untrusted input;
  `exists()` counts only complete record pairs;
  `list_employee_ids()` skips corrupt sidecars instead of crashing.
  The constructor's `expected_dimension` (default `0` = any positive
  dimension) is enforced on both save and load, making cross-model
  dimension compatibility explicit (`EmbeddingRecordFormatError`).
* Safety: save validates the vector (1-D, non-empty, floating, finite,
  non-zero norm) and the ID **before any write**; a record is never
  overwritten without the explicit `replace=True` flag (files staged,
  flushed, fsync-ed; hard-link publication fails loudly on a
  concurrent winner; publication order array-then-sidecar so a crash
  can leave an unpublished array but never a metadata-declared record
  without its validated sidecar); failed attempts clean only their
  own staged/unpublished artifacts. Typed errors:
  `EmbeddingStorageInputError` (ValueError),
  `DuplicateEmbeddingRecordError`, `EmbeddingRecordNotFoundError`,
  `CorruptEmbeddingRecordError`, `EmbeddingRecordFormatError`,
  `EmbeddingPersistenceError`. Returned records are frozen with
  write-protected vectors. Import/construction perform no filesystem
  work (directories created only when a real save needs them).
* `tests/test_embedding_storage.py`: 79 deterministic tests on
  synthetic vectors in temporary directories (no real biometrics, no
  network): round trips, exact-ID preservation, vector validation
  (NaN/±inf/zero-norm/shape/dtype), duplicate refusal + explicit
  replace, corruption matrix (garbage files, invalid JSON, object
  arrays, smuggled pickle bytes that must never execute, on-disk
  NaN, dtype/shape/dimension/ID mismatches, naive timestamps,
  unknown dtype markers, incompatible schema versions), traversal
  and malicious-ID rejection before any write, write-failure cleanup
  with unrelated data untouched, hygiene (import/construction/
  foreign-CWD/fresh-subprocess/no-device-no-network), and output
  isolation.

### Verified

* Focused embedding-storage tests: 79 passed.
* `python -m pytest` → **421 passed** (342 prior + 79 new) from the
  project root and an external working directory; stable across
  repeated runs.
* `git diff --check` → clean.
* No biometric artifacts in the repository (tests use temp dirs).

### Known limitations

* Storage does not consult enrollment manifests (deliberate — no
  second identity source-of-truth); recognizing only enrolled
  employees is Step 10's gating decision.
* Crash between the two file publications leaves an orphan array
  (never a declared record without metadata); orphans are skipped by
  `exists()`/`list_employee_ids()` but not garbage-collected.
* Model identity/version is not recorded (SFace exposes no version
  API); dimension compatibility is the explicit contract instead.
* Not included per plan: matching/recognition (Step 10), temporal
  verification, unknown-face handling, attendance logic.

## [0.8.0] — 2026-10-11 — Phase 1, Step 08: Face embedding generation

### Added

* `src/face_embedding.py`: `FaceEmbedder` — converts one BGR uint8
  face crop (the exact format Step 07 persists) into a validated
  float32 1-D embedding. Explicit `load()`/`is_loaded`/`embed()`
  lifecycle; import and construction are side-effect-free;
  `backend_factory` injectable (tests use fakes; the real default is
  `cv2.FaceRecognizerSF`, bundled in the pinned opencv-python wheel —
  no new dependency, no `onnxruntime`). Typed errors:
  `FaceEmbeddingInputError` (ValueError) / `FaceEmbeddingModelError`
  (RuntimeError) / `FaceEmbeddingOutputError` (RuntimeError).
  Validated frozen `FaceEmbedding` result: write-protected vector,
  dimension, pre-normalization L2 norm, explicit dtype marker.
* Output contract: 1-D, finite, dimension validated against the
  configured `expected_dimension` (0 = accept any non-empty),
  zero-norm rejected (never divided), L2 normalization only when
  `normalize_output=True` (contract-driven, not convention).
* `tests/test_face_embedding.py`: 33 deterministic tests on
  injected fakes — input contract, lifecycle, generation/output
  validation (NaN/inf/zero-norm/shape), output isolation,
  hygiene (import/construction/foreign-CWD/fresh-subprocess), and
  Step 07 PNG-crop format roundtrip compatibility.

### Model approach

* **SFace** (OpenCV Zoo `face_recognition_sface_2021dec.onnx`) chosen
  over insightface buffalo: buffalo's ArcFace path requires five
  landmarks that Step 07 crops do not carry; SFace's documented
  crop→112×112 preprocessing works on plain crops; Apache-2.0 (vs
  research-only buffalo); bundled DNN loader in the pinned wheel.
* The SFace artifact is **operator-supplied** at `models/sface.onnx`
  and **never downloaded**; until provided, `load()` raises
  `FaceEmbeddingModelError`. **Real-model inference is pending**
  (no artifact in this repository; unit tests are explicitly
  fake-backed).

### Verified

* Focused embedding tests: 33 passed.
* `python -m pytest` → **342 passed** (309 prior + 33 new) from the
  project root and an external working directory; stable across
  repeated runs.
* `git diff --check` → clean.

### Known limitations

* Crop-only (no landmark alignment) SFace accuracy is unmeasured;
  Phase 2 evaluation on real footage required before any accuracy
  claim.
* Not included per plan: embedding persistence (Step 09),
  matching/recognition (Step 10), liveness, attendance logic.

## [0.7.0] — 2026-10-11 — Phase 1, Step 07: Multi-image face enrollment

### Added

* `src/enrollment.py`: `EnrollmentService` — one controlled, bounded
  capture session per employee: identity validation via the existing
  `Employee` model (IDs preserved exactly, `007`/`EMP-0042`),
  duplicate-refusal before any capture, camera/detector/assessor
  orchestration through the existing Step 04–06 services, per-sample
  accept/reject with explicit typed reasons, and staged persistence.
  Typed errors: `EnrollmentInputError`, `DuplicateEnrollmentError`,
  `EnrollmentPersistenceError`. Results: frozen `EnrollmentResult` with
  COMPLETED/CANCELLED/FAILED status, accepted `SampleRecord`s,
  per-frame `SampleRejection`s (reason + measured quality), attempt
  count and duplicate-skip count. Camera released on every exit path;
  cancellation and failure never persist; persistence failures clean
  up only current-attempt artifacts. Camera, detector, assessor,
  config, and settings are injectable (hardware-free tests).
* `src/config.py`: `EnrollmentConfig.max_capture_attempts=60` — the
  bound preventing endless capture loops when frames keep failing;
  validated `>= min_samples`.
* Storage layout (both roots already git-ignored):
  `<data>/employees/<encoded-id>/employee.json` (schema version 1;
  full Employee serialization incl. `is_active`, sample count,
  per-sample file/UTC time/quality metrics) and
  `<data>/face_samples/<encoded-id>/sample_001.png`. `<encoded-id>` is
  a deterministic percent-encoding of the exact ID (dots encoded),
  making path traversal structurally impossible. Existing enrollments
  are never overwritten; samples stage in `.staging-` dirs, are
  verified by re-decoding, and publish samples-first,
  manifest-last.
* `tests/test_enrollment.py`: 38 deterministic tests with fakes and
  synthetic frames — validation, duplicate refusal, capture flow,
  cancellation, camera lifecycle/errors, service exceptions, attempt
  budget, byte-duplicate skipping, persistence layout/content,
  path-safety encoding, failure cleanup, and import hygiene.

### Verified

* Focused enrollment tests: 38 passed.
* `python -m pytest` → **309 passed** (271 prior + 38 new) from the
  project root and an external working directory; stable across
  repeated runs.
* Fresh-interpreter/foreign-CWD hygiene: import and service
  construction create no directories and touch no camera/model/files.
* `git diff --check` → clean.

### Known limitations

* Near-duplicate biometric similarity is out of scope (needs
  embeddings, Step 08); only exact byte-duplicate suppression exists.
* Multi-file persistence is best-effort atomic, not transactional: a
  crash between sample and manifest publication can leave orphan
  sample files — never a declared enrollment without images.
* Capture/sampling thresholds are initial defaults; Phase 2
  calibration pending.

### Not included (per plan — later steps)

* Embeddings (Step 08), recognition (Step 10), liveness, attendance
  logic, CLI/GUI enrollment UX.

## [0.6.0] — 2026-10-10 — Phase 1, Step 06: Face quality assessment

### Added

* `src/quality.py`: `FaceQualityAssessor` — deterministic per-face
  image-quality gating plus the frame face-count policy.
  * `assess_face(frame, detection)`: measured metrics (usable box size,
    Laplacian-variance sharpness, grayscale mean brightness) plus an
    optional landmark orientation proxy, returning a frozen `FaceQuality`
    with an explicit `FaceQualityDecision` (ACCEPT/REJECT) and a stable,
    machine-readable `FaceQualityReason` for the first-failed check
    (stable order: out_of_frame → too_small → no_landmarks/pose → blur
    → too_dark → too_bright). Rejections still carry the measured
    metrics; unusable inputs (fully-out-of-frame box) reject with all
    metrics `None` — nothing is invented.
  * `assess_frame_faces(detections)`: face-count policy kept separate
    from image quality — `no_faces` / `multiple_faces` / `None`. Even a
    policy-rejected multi-face frame leaves every face individually
    assessable for future multi-face use.
  * Errors mirror Step 05 conventions: `FaceQualityInputError`
    (a `ValueError`) for contract-violating inputs; typed reasons for
    valid inputs that fail quality gates. Frames and detections are
    never mutated; no camera/model/file/network surface.
* `src/config.py`: new frozen `QualityConfig` + project-wide `QUALITY`
  instance (Step 02 convention: tune thresholds in one place).
  Defaults are initial calibration starting points, not proven
  biometric cutoffs: `min_face_size_px=80`, `min_sharpness=50.0`,
  brightness window `[40, 220]`, `pose_yaw_tolerance=0.15`,
  `pose_pitch_tolerance=0.35`, `pose_policy="optional"|"required"`,
  `require_single_face=True`. Fail-fast `ValueError` validation.
* `src/face_detection.py` (backward-compatible): `FaceDetection` gains
  an optional `landmarks` field — five `(x, y)` points in canonical
  YuNet order, populated only from real 15-column backend rows, `None`
  otherwise; non-finite landmark values drop the landmarks, keep the
  box. Legacy code constructing `FaceDetection` without landmarks is
  unaffected.
* `tests/test_quality.py`: 56 deterministic tests with synthetic
  images (private-seeded RNG so expected metric values are
  order-independent) and real `cv2` metric computation; plus 4
  landmark regression tests in `tests/test_face_detection.py`.

### Verified

* Focused quality tests: 56 passed. Face-detection tests: 46 passed.
* `python -m pytest` → 271 passed (215 prior + 56 new) from the project
  root and an external working directory; stable across repeated runs.
* Fresh-process import hygiene: no camera, no model init, no state at
  import or assessor construction; no unrelated ML stack loaded.
* `git diff --check` → clean.

### Known limitations

* The orientation check is a geometric landmark proxy — **not** 3D
  pose estimation; cannot distinguish head rotation from facial-shape
  variation. Without landmarks (`pose_policy="optional"`) the check is
  skipped and the pose metrics stay `None`.
* Thresholds are initial, testable defaults; calibration on real
  enrollment footage is deferred to Phase 2 evaluation.

### Not included (per plan — later steps)

* Enrollment itself (Step 07), embeddings (Step 08), recognition,
  temporal verification, attendance logic, liveness, multi-frame
  quality decisioning.

## [0.2.0] — 2026-09-23 — Phase 1, Step 02: Configuration & constants

### Added

* `src/config.py`: centralized configuration and constants.
  * Frozen application constants: `APP_NAME`, `PROJECT_ROOT`
    (cwd-independent, derived from `__file__`), `CAMERA`,
    `FACE_PROCESSING`, `ENROLLMENT` (validated frozen dataclasses —
    the single place to tune thresholds in later steps).
  * Environment-specific `AppSettings` via `load_settings()`:
    `APP_ENV` (development/testing/staging/production), `DEBUG` (strict
    boolean), `DATA_DIR`, `LOG_DIR` (relative values anchored to the
    project root) plus derived paths (`employees_dir`,
    `face_samples_dir`, `embeddings_dir`, `models_dir`, `reports_dir`).
  * `ConfigError(ValueError)` fail-fast handling for invalid environment
    values; invalid constants raise `ValueError` at import.
  * `ensure_directories(settings)` for explicit directory creation —
    importing the module performs no side effects.
* `.env.example`: added `APP_ENV` and `DEBUG`; documented the
  environment-vs-constants split (camera/face/enrollment constants stay
  in `src/config.py`).
* `tests/test_config.py`: 18 tests mapped to the six Step 02
  requirements (import surface, path resolution incl. foreign-cwd
  subprocess, value availability and frozen-ness, env overrides,
  invalid-value handling, import side-effect freedom).

### Verified

* `python -m pytest` → 35 passed (17 Step 01 + 18 Step 02), from both
  the project root and an external cwd.
* Fresh-interpreter check: importing `src.config` imports no CV/ML stack
  and creates no directories.

### Not included (per plan — later steps)

* Camera service, detection, quality, enrollment logic, embeddings,
  recognition, temporal verification, attendance logic.

## [0.3.0] — 2026-10-09 — Phase 1, Step 03: Employee data model

### Added

* `src/employee.py`: the identity-layer `Employee` model.
  * `Employee` — frozen, slotted dataclass with `employee_id`,
    `full_name`, `is_active`, `created_at` and `to_dict()` explicit
    serialization (dictionary only — not persistence).
  * Validation: `EmployeeValidationError(ValueError)` with actionable,
    field-specific messages; no silent coercion of invalid input.
  * Normalization: `employee_id` trimmed only (entries like `"007"` and
    `"EMP-0042"` preserved exactly); `full_name` trimmed plus internal
    whitespace collapsed, case preserved; `is_active` must be a real
    `bool`; `created_at` defaults to UTC now and must be timezone-aware.
  * Identity boundaries honored: no face data, embeddings, camera
    frames, recognition confidence, or attendance state in the model;
    no CV/ML/database dependencies (ARCHITECTURE.md D6).
* `tests/test_employee.py`: 36 tests mapped to the nine Step 03
  requirements (creation, field validation, whitespace rejection,
  boolean strictness, normalization, ID preservation, timestamps,
  serialization, import hygiene).

### Verified

* `python -m pytest` → 71 passed (35 Step 02 + 36 Step 03) from the
  project root.
* External-cwd run → 71 passed.
* Fresh-interpreter check: importing `src.employee` imports no CV/ML
  stack and performs no side effects.

### Not included (per plan — later steps)

* Employee persistence/repository and `employee_id` uniqueness
  enforcement (storage steps + Phase 3).
* Camera service, detection, quality, enrollment logic, embeddings,
  recognition, temporal verification, attendance logic.

## [0.4.0] — 2026-10-09 — Phase 1, Step 04: Live camera service

### Added

* `src/camera.py`: `CameraService` — the frame source for downstream
  face detection.
  * `open()` raises `CameraOpenError` if the device cannot be opened or
    configured framing cannot be applied; `read_frame()` returns
    `(True, frame)` only for verified non-empty device frames and
    `(False, None)` on failure; `release()` is idempotent and never
    raises; `is_open` reflects actual device state.
  * Context-manager support: `__enter__` performs the open,
    `__exit__` always releases the capture — even if the `with` block
    raises — and never masks the in-flight exception.
  * `CameraStateError` for invalid lifecycle misuse (reading/opening
    in the wrong state); `CameraError` is the common base.
  * Configuration comes solely from `src.config.CAMERA` (index,
    frame dimensions); logging reuses the Step 01 `get_logger`
    contract.
* `tests/test_camera.py`: 30 hardware-independent tests with
  deterministic `cv2.VideoCapture` fakes covering: success/failure of
  open and read, capture release and idempotent release, context-manager
  cleanup on normal and exceptional exit, no handle leak on failed
  open, no fabricated/stale frames, configured index/framing honored,
  custom `CameraConfig` injection, lifecycle-state semantics, and
  import hygiene (no `VideoCapture` instantiation at import).

### Verified

* Focused camera tests: 30 passed.
* `python -m pytest` → 101 passed (71 prior + 30 new), from both the
  project root and an external working directory.
* Fresh-interpreter checks: importing `src.camera` instantiates no
  `cv2.VideoCapture` and imports no ML stack.

### Not included (per plan — later steps)

* Face detection, quality assessment, enrollment, embeddings,
  recognition, attendance logic; camera reconnection/background
  streaming/multi-camera management by design.

## [0.5.0] — 2026-10-09 — Phase 1, Step 05: Face detection

### Added

* `src/face_detection.py`: `FaceDetector` — locating faces in
  caller-supplied frames (no identification, no camera ownership).
  * Backend: `cv2.FaceDetectorYN` (YuNet ONNX) from the already-pinned
    opencv-python dependency — **no new dependency added**. Verified
    empirically that opencv-python 5.0.0.93 ships no cascade files and
    no YuNet model; the ONNX model is operator-supplied
    (`models/yunet.onnx`, never auto-downloaded, never auto-created).
  * `FaceDetector.load()` is explicit and idempotent;
    `FaceDetectionInputError` (a `ValueError`) covers invalid frames
    (None/empty/wrong-dim/channels/dtype/zero-size); frames are never
    mutated; `FaceDetectorLoadError` covers missing/corrupt models;
    `FaceDetectorError` covers backend failures — failures are never
    misreported as "no faces".
  * `FaceDetection` frozen result: `x, y, width, height` pixel box plus
    backend-supplied `confidence` (None when absent, never invented),
    clamped to frame bounds, fully-outside boxes dropped.
  * Detection threshold comes from `FACE_PROCESSING.detection_confidence`
    (Step 02); nothing hardcoded.
* `tests/test_face_detection.py`: 42 deterministic tests (typed stub
  backend for correctness paths, real-backend failure integration for
  missing/corrupt models, fresh-process import hygiene — no camera, no
  model load, no network, no filesystem writes at import).

### Verified

* Focused face-detection tests: 42 passed.
* `python -m pytest` → 143 passed (101 prior + 42 new), from both the
  project root and an external working directory.

### Not included (per plan — later steps)

* Face quality assessment, enrollment, embeddings, recognition,
  temporal verification, attendance logic. The YuNet model file itself
  must be supplied by the operator before real-world detection runs.

## [0.1.0] — 2026-09-23 — Phase 1, Step 01: Project structure & environment

### Added

* Git foundation: `.gitignore` covering virtual environments, `.env`
  secrets, biometric data (`data/employees/`, `data/face_samples/`,
  `data/embeddings/`), runtime logs, and model weights.
* Python 3.14 virtual environment; `requirements.txt` with pinned
  foundation packages: `numpy 2.5.3`, `opencv-python 5.0.0.93`,
  `python-dotenv 1.2.3`, `pytest 9.1.1`. All wheels install successfully
  on Python 3.14 (Windows x64).
* Project directory skeleton: `src/`, `notebooks/`, `data/{employees,
  face_samples,embeddings,logs}/`, `models/`, `tests/`, `reports/`,
  `docs/` (placeholders via `.gitkeep`).
* `src/logging_setup.py`: `get_logger(name)` providing an idempotent
  console + rotating-file logging setup (`data/logs/app.log`, 5 MB × 5),
  configured via `LOG_LEVEL` / `LOG_DIR`.
* `.env.example` documenting the environment keys actually consumed by
  the implemented code.
* `tests/test_environment.py`: 17 verification tests (venv detection,
  Python version, package imports, logging behavior, directory skeleton).
* `pytest.ini` with `pythonpath = .` for invocation-independent imports.
* Project documentation: `README.md`, `PROJECT_PLAN.md`,
  `ARCHITECTURE.md` (incl. decisions D1–D4), `DEVELOPMENT_STATUS.md`.

### Verified

* `pytest tests/ -v` → 17 passed (Python 3.14.7, venv interpreter).
* Bare `pytest` and external-cwd invocation → 17 passed.
* `git check-ignore` confirms generated/biometric paths are excluded.

### Not included (per plan — later steps)

* Camera capture, face detection, quality, enrollment, embeddings,
  recognition, temporal verification, attendance logic.
* Phases 2–7 (business rules, PostgreSQL/FastAPI, dashboard, RBAC,
  Docker, advanced liveness).
