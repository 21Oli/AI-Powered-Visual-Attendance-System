# Phase 4 — Implementation plan and milestone status

Owner-approved direction: Next.js App Router, TypeScript (strict), CSS Modules,
CSS custom properties, no Tailwind, no UI framework, dark graphite + teal with a
light theme, and a same-origin Route Handler proxy so FastAPI keeps CORS
disabled.

---

## 1. Milestones

| # | Milestone | Status |
|---|---|---|
| P4.1 | Repository inspection, architecture and dependency approval list | **PASSED** (owner-approved) |
| P4.2 | Frontend scaffold and design system | **PASSED** (owner-approved, `e3ead4d`) |
| P4.3 | Application shell and navigation | **PASSED** (owner-approved, `3d575d3`) |
| P4.4 | Typed API client, secure proxy, fixtures and Overview | **VERIFIED** — committed `e68da71`, re-verified in P4.6.6 |
| P4.5 | Employees screen (list, create, detail, rename, deactivate) | **VERIFIED** — committed `376345d`, re-verified in P4.6.6 |
| P4.6 | Attendance screen (journal, checkout confirmation) | journal + operator check-out **VERIFIED** — committed `0a8fd55`/`c0e5e77`/`3b54163`/`52a6ded`, re-verified in P4.6.6 |
| P4.7 | System status screen (API health, offline states) | **COMPLETED** — committed `276302f` (liveness-only health screen) |
| P4.8 | Remaining API surfaces (writes) and hardening | completed — requirements satisfied by P4.5–P4.7; no additional implementation required |
| P4.9 | Final verification, documentation and acceptance review | not started |

Every milestone stops for explicit owner approval.

---

## 2. Dependencies (unchanged since P4.2)

`next 16.4.6`, `react`/`react-dom 19.3.1`; dev: `typescript 5.9.3`,
`@types/node 24.19.2`, `@types/react`/`@types/react-dom 19.3.0`,
`eslint 10.14.0`, `eslint-config-next 16.4.6`, `vitest 5.1.0`, `vite 8.3.8`,
`jsdom 31.0.0`, `@testing-library/react 16.3.3`, `@testing-library/dom 10.4.2`,
`@testing-library/user-event 14.6.9`.

P4.4 installed, upgraded and removed **nothing**. Parsing, validation, transport,
charts and icons were all met with existing packages or hand-written code.

---

## 3. P4.4 scope and architecture

```text
Browser → OverviewDashboard (client) → src/lib/api/client.ts
        → /api/backend/[...path] (Route Handler) → BACKEND_BASE_URL
        → FastAPI (loopback) → PostgreSQL / JSONL journal
```

| Layer | Files |
|---|---|
| Contract + validation | `src/lib/api/types.ts` |
| Errors | `src/lib/api/errors.ts` |
| Transport | `src/lib/api/client.ts` |
| Endpoints | `src/lib/api/employees.ts`, `src/lib/api/attendance.ts` |
| Source selection | `src/lib/api/data-source.ts` |
| Fixtures | `src/lib/fixtures/overview-fixtures.ts` |
| Calculations | `src/lib/dashboard/overview.ts` |
| Proxy policy | `src/lib/server/proxy.ts` |
| Proxy entry point | `src/app/api/backend/[...path]/route.ts` |
| UI | `src/components/dashboard/{OverviewDashboard,MetricCard}` |

**Verified contract corrections** (source, not assumption): `event_type` is
lowercase `"check_in"` / `"check_out"`; `occurred_at`/`created_at` are UTC ISO via
`isoformat()`; **`created_at` is nullable**; the employee list `count` is the page
length, never a total; attendance has no pagination; checkout accepts no body.

**Proxy allowlist** — exactly the eight published pairs. Query surface is
`after`/`limit` on the employee list only. Identifiers must be non-empty, ≤128
chars, trimmed, not `.`/`..`, and free of `/ \ ? # %` and control characters;
they are re-encoded once when forwarded. The upstream URL is rebuilt from the
matched route and a validated origin, so no client input can choose a
destination. Redirects are never followed; bodies are forwarded only for the two
write routes and capped at 4 KiB; failures return sanitized `502`/`504`/`500`
codes with no origin, stack trace or payload.

**Fixture isolation**: `resolveDataSourceKind` requires
`NODE_ENV === "development"` **and** `DASHBOARD_DATA_MODE=fixtures`, and runs on
the server (`force-dynamic` page), so a production build can never serve fixtures
and the browser cannot enable them. The UI shows a persistent
`FIXTURE DATA — DEVELOPMENT ONLY` banner. There is no fallback from a failed live
request to fixtures.

**Metric definitions**

| Metric | Rule |
|---|---|
| Total employees | Sum of every keyset page (`limit=100`, follow `next_after` to `null`); page `count` is never used as a total |
| Active employees | Records with `is_active === true` |
| Events today (UTC) | Events whose `occurred_at` UTC day equals the current UTC day (labelled UTC deliberately) |
| Open sessions | Latest event per employee by `(occurred_at, journal position)`; open when it is `check_in`; ties break to the later journal position |
| Journal integrity | `incomplete_trailing_record`, surfaced as a warning; the journal is never modified |

Safeguards: repeated cursor → `PAGINATION_CURSOR_REPEATED`; over-long walk →
`PAGINATION_LIMIT_REACHED`; a failed page fails the metric instead of showing a
partial total; unreadable timestamps and unknown event types are excluded and
disclosed.

---

## 4. Verification (P4.4)

| Command | Result |
|---|---|
| `npm ls --depth=0` | exit 0 — 15 packages, unchanged |
| `npm run lint` | exit 0 — 0 problems |
| `npm run typecheck` | exit 0 — strict |
| `npm run test` | exit 0 — **184 passed / 184**, 12 suites, ~32 s |
| `npm run build` | exit 0 — static `/`, `/_not-found`, `/attendance`, `/employees`, `/system-status`; dynamic `/overview`, `/api/backend/[...path]` |

Test growth 85 → 184 (+99): proxy (38), client (12), fixtures (14), metrics (25),
Overview UI (11), plus the updated route suite.

Three first-run failures were corrected in the tests, not by weakening code: two
cards legitimately display the same number (queries are now scoped per card), the
dashboard does not own the theme (the test now pre-applies it), and Next hands
the handler already-decoded path segments (the proxy test now passes the decoded
form). No lint rule was disabled and no dependency added.

---

## 5. P4.5 scope and verification

```text
Browser → EmployeesView (client) → src/lib/api/employees.ts
        → /api/backend/[...path] (unchanged proxy) → FastAPI (unchanged)
```

| Layer | Files |
|---|---|
| Form rules | `src/lib/employees/validation.ts` |
| Display formatting | `src/lib/employees/format.ts` |
| Failure → field mapping | `src/lib/employees/errors.ts` |
| Endpoints (added) | `createEmployee`, `fetchEmployeeById`, `renameEmployee`, `deactivateEmployee` in `src/lib/api/employees.ts` |
| Primitives | `src/components/ui/{TextField,ConfirmDialog,StatusMessage}` |
| Screen | `src/components/employees/{EmployeesView,EmployeeTable,PaginationControls,CreateEmployeeForm,EmployeeDetail}` |

**Employee-ID compatibility (owner-approved Option A).** `validation.ts`
imports `isSafeEmployeeId` from `src/lib/server/proxy.ts` rather than
re-implementing it, so the form's rule is the proxy's rule by construction. A
value is submitted unchanged (no trimming or case folding); leading zeros,
hyphens and mixed case remain valid. Existing records with ids the proxy cannot
address stay visible with their actions disabled and the reason stated.

**Pagination.** `Next` uses `next_after`; `Previous` replays a client-side cursor
history. Page sizes 10/20/50/100 (backend max 100) reset to page 1 on change. A
full final page followed by an empty page is rendered as such, not as an error.
`count` is the page length and is never shown as a total; the API has no search
or sorting, so neither is offered. Requests carry an id and an `AbortController`,
so a stale response cannot replace a newer page.

**Deactivation** requires an explicit confirmation naming the employee and
stating that no reactivation endpoint exists; success is shown only after the
response, then the directory is refetched. The dialog moves focus to Cancel,
traps Tab, closes on Escape and restores focus to the trigger.

| Command | Result |
|---|---|
| `npm ls --depth=0` | exit 0 — 15 packages, unchanged |
| `npm run lint` | exit 0 — 0 problems |
| `npm run typecheck` | exit 0 — strict |
| `npm run test` | exit 0 — **249 passed / 249**, 19 suites |
| `npm run build` | exit 0 — static `/employees`; dynamic `/overview`, `/api/backend/[...path]` |

Test growth 184 → 249 (+65) across seven new suites (employee-ID and formatting
rules, error mapping, API client, directory and pagination, create, detail/rename,
deactivation) plus the restructured route suite. Four first-run failures in two
suites were test-side mistakes (the required asterisk inside every label broke
exact label matching, a leading space cannot be produced by `user.type`, and a
field hint also matched an error assertion); the queries were fixed, not the
assertions weakened.

---

## 5a. P4.6.4 scope and verification (read-only Attendance)

```text
Browser → AttendanceView (client) → createOverviewSource(dataSourceKind)
        → src/lib/api/{attendance,employees}.ts
        → /api/backend/[...path] (unchanged proxy) → FastAPI (unchanged)
```

| Layer | Files |
|---|---|
| Journal fold / session state | `src/lib/attendance/journal.ts` (P4.6.2, reused) |
| Filters | `src/lib/attendance/filters.ts` (P4.6.2, reused) |
| Client-side paging | `src/lib/attendance/pagination.ts` (P4.6.2, reused) |
| Display formatting | `src/lib/attendance/format.ts` |
| Summary and UTC days | `src/lib/attendance/summary.ts` |
| Screen | `src/components/attendance/{AttendanceView,AttendanceTable,AttendanceFilters,AttendancePagination}` |
| Route | `src/app/(dashboard)/attendance/page.tsx` (dynamic) |

**History is read-only by construction.** The journal table has no mutation
control and nothing edits, inserts or re-sorts an event. P4.6.5 adds exactly one
write — the confirmed operator check-out described below — and no other control
on the page sends anything.

**Loading.** The journal and the complete employee directory load concurrently
through the existing `createOverviewSource` and `Promise.allSettled`, so an
attendance failure never erases employee success or vice versa. The walk follows
`next_after` until `null`; a directory failure is reported as unavailable rather
than as a short but complete list. Requests are abortable and counter-tagged, so
a stale response cannot overwrite a newer refresh, and a live failure never falls
back to fixtures.

**Display.** Rows keep journal append order and are never sorted. Timestamps are
`YYYY-MM-DD HH:MM:SS UTC`, unreadable ones read `Unavailable`, unknown event types
remain visible as `Unknown type (…)`, and the session column shows the employee's
current derived state (latest valid timestamp, ties to the later journal
position) rather than a session per row. An unresolved id shows the id with an
explicit label; no name is fabricated.

**Filters and paging.** Day, type, employee and session status compose through
`filterAttendanceEvents`; `Reset filters` restores the defaults. Paging defaults
to 20 of 10/20/50/100, resets on a filter or size change, and clamps when a
refresh returns fewer events. Both are client-side: the API has no attendance
query parameters.

**Fixtures.** `fixtureAttendance` now yields 25 synthetic events across three UTC
days (`FIX-001`…`FIX-006`), an inactive employee with a historical open session
and one undated record. The gate is unchanged: `NODE_ENV === "development"` and
`DASHBOARD_DATA_MODE=fixtures`, resolved server-side, so production can never
serve it.

| Command | Result |
|---|---|
| `npm ls --depth=0` | exit 0 — 15 packages, unchanged |
| `npm run lint` | exit 0 — 0 problems |
| `npm run typecheck` | exit 0 — strict |
| `npm run test` | exit 0 — **439 passed / 439**, 29 suites (P4.6.5) |
| `npm run build` | exit 0 — dynamic `/attendance`; static `/employees`, `/system-status` |

**P4.6.5 addendum.** `AttendanceCheckout` reuses `checkoutEmployee`,
`classifyCheckoutError`, the shared `ConfirmDialog`/`StatusMessage` primitives and
`isManageableEmployeeId`; `src/lib/attendance/checkout.ts` holds the eligibility
matrix and the failure copy. One request is in flight at most (guarded by a ref as
well as state), the POST carries no body and therefore no `Content-Type`, nothing
is optimistic, and a confirmed success reloads the journal so the session closes
only because the service recorded it. A `503 PERSISTENCE_UNCONFIRMED` is labelled
uncertain, never retried and never described as success or definite failure; it
offers an explicit journal refresh for verification. Fixture mode offers no action
at all.

Test growth 352 → 398 (+46) across three new attendance suites and the updated
route suite. The only first-run failure that needed a source change (rather than
a test query) was the pre-existing fixture determinism test, which correctly
requires two different clocks to produce different data; a record is now placed
at the fixture clock instant instead of weakening that assertion.

---

## 6. Remaining work and constraints

* P4.7 replaced the System Status placeholder with a backend-API liveness
  screen. The Attendance journal is implemented read-only and P4.6.5 added the
  single approved write: a confirmed operator check-out for an active employee
  with an open session in live mode. There is still no check-in, bulk or
  automatic check-out. P4.8 is closed as satisfied by P4.5–P4.7 (§9); P4.9 is
  not started.
* **No live verification.** The backend's G2+G8 startup gate requires
  PostgreSQL; all proxy/Overview evidence is from mocked responses and
  development fixtures. No browser was driven either.
* Attendance has no server pagination, so the screen downloads the whole journal
  and pages it in the browser; events carry no employee name, so names come from
  walking every `GET /employees` page and are never inferred.
* Checkout accepts no body; the service enforces its configured cooldown
  (300 seconds by default) and stays authoritative, so the UI explains the
  cooldown without gating on a browser-side timer.
* An identifier containing `%`, `#`, `?`, `/` or `\` is refused by the proxy by
  design; ids are capped at 128 characters and may not have surrounding
  whitespace. Such records can be listed but not managed, which is displayed
  rather than hidden.
* The Employees screen has no fixture mode: the development fixture gate is used
  by the Overview and Attendance screens only.
* No authentication or RBAC until Phase 5.

---

## 7. P4.6.6 final Phase 4 review (2026-11-12)

Review-only milestone: no source behaviour, proxy rule, API contract, backend
file, database file or dependency changed. Three stale statements were corrected
from evidence: the `fixtureAttendance` doc comment (said 26 events and listed
FIX-003 as open; it builds 25 events and FIX-003 is closed), `next.config.ts`
(claimed the proxy "arrives in P4.8"; it arrived in P4.4), and documentation that
still described the page as checkout-free or fixture mode as Overview-only, plus
stale test counts.

Verified on the committed tree (`52a6ded`, clean working tree):

| Command | Result |
|---|---|
| `npx vitest run` | exit 0 — **439 passed / 439**, 29 suites, 70.8 s |
| `npm run lint` | exit 0 — 0 problems |
| `npx tsc --noEmit` | exit 0 — strict |
| `npm run build` | exit 0 — static `/`, `/_not-found`, `/employees`, `/system-status`; dynamic `/attendance`, `/overview`, `/api/backend/[...path]` |
| `npm ls --depth=0` | exit 0 — the same 15 pinned packages |

**Phase 4 status at P4.6.6: P4.1–P4.6.5 were implemented and committed, with
automated verification passing; P4.7 was still a placeholder, so that review
recorded `PHASE 4 INCOMPLETE — BLOCKERS REMAIN`.** P4.7 has since been
implemented (§8) and P4.8 is closed by documentation bookkeeping (§9); **P4.9
remains and Phase 4 is still incomplete.** Live backend (PostgreSQL-gated),
real-browser and deployment validation are still unperformed, so nothing here is
a production-readiness claim.

---

## 8. P4.7 scope and verification (System Status)

```text
Browser → SystemStatusView (client) → src/lib/api/health.ts
        → /api/backend/[...path] (unchanged proxy, GET /api/v1/health)
        → FastAPI health handler (liveness only)
```

| Layer | Files |
|---|---|
| Health client | `src/lib/api/health.ts` (reuses `requestJson`, `API_PATHS.health`, `parseHealth`) |
| Screen | `src/components/system-status/SystemStatusView.tsx` + CSS Module |
| Route | `src/app/(dashboard)/system-status/page.tsx` (dynamic, server-resolved fixture mode) |

**What the endpoint actually is.** The FastAPI handler returns
`HealthResponse()` — `{ "status": "ok" }` — with no database access; its own
docstring calls it a liveness claim, not a readiness claim, and names
`GET /api/v1/employees` as the honest database probe. The screen repeats that
scope and lists PostgreSQL/schema/migrations, cameras, recognition accuracy,
biometric storage, authentication and deployment readiness as **not** verified.
No endpoint was added, and the proxy allowlist is unchanged (the health pair was
already permitted).

**States.** One manual check at a time: loading (`role="status"`, Refresh
disabled), healthy only after a validated `{ "status": "ok" }`, unreachable
backend, timeout (`HEALTH_TIMEOUT`, 8-second `AbortSignal.timeout` combined with
the caller's signal so unmount is not a timeout), unexpected response (malformed
JSON, wrong shape, unexpected HTTP status) and server error (5xx / sanitized
proxy failures). A failure replaces the previous result rather than inheriting
its healthy state, and nothing retries or polls automatically.

**Fixture mode.** Server-resolved, disclosed with the standard banner; no
request is made, no status is displayed and no healthy backend is fabricated. A
failed live check still never falls back to fixtures.

| Command | Result |
|---|---|
| `npm ls --depth=0` | exit 0 — 15 packages, unchanged |
| `npx vitest run` | exit 0 — **457 passed / 457**, 31 suites |
| `npm run lint` | exit 0 — 0 problems |
| `npx tsc --noEmit` | exit 0 — strict |
| `npm run build` | exit 0 — dynamic `/system-status`; static `/`, `/_not-found`, `/employees` |

Test growth 439 → 457 (+18) across `health-api.test.ts` and
`system-status.test.tsx`, plus the updated route suite. One first-run lint error
(synchronous `setState` inside the mount effect) was fixed by moving the
loading/attempt reset into the Refresh handler and initialising the first attempt
with a lazy state initialiser — no rule was disabled.

---

## 9. P4.8 closure — documentation only (owner-approved)

P4.8 is titled "Remaining API surfaces (writes) and hardening". Repository review
found no unimplemented requirement behind that title: the backend publishes
exactly eight route/method pairs, every one already had a typed frontend client
and a screen, all four write operations already had complete workflows, and the
proxy already enforced the approved security rules with tests. The owner therefore
closed P4.8 by **documentation bookkeeping**, not by new code:

> **P4.8 status: COMPLETED — REQUIREMENTS SATISFIED BY P4.5–P4.7.**

No source, test, proxy, contract or dependency changed in this closure.

### Existing API coverage (previously implemented, not new P4.8 features)

| Backend route | Existing frontend implementation |
|---|---|
| `GET /api/v1/health` | `src/lib/api/health.ts` and the System Status screen (P4.7) |
| `POST /api/v1/employees` | employee creation form (P4.5) |
| `GET /api/v1/employees` | employee directory with keyset pagination (P4.5), and the full walk used for names and filters |
| `GET /api/v1/employees/{employee_id}` | employee detail panel (P4.5) |
| `PATCH /api/v1/employees/{employee_id}` | employee rename, full name only (P4.5) |
| `POST /api/v1/employees/{employee_id}/deactivate` | confirmed employee deactivation (P4.5) |
| `GET /api/v1/attendance` | Overview metrics (P4.4) and the Attendance journal (P4.6) |
| `POST /api/v1/attendance/{employee_id}/checkout` | confirmed operator-authorized check-out (P4.6.5) |

The four write operations — employee creation, employee rename, employee
deactivation and operator-authorized attendance check-out — were all implemented
before P4.8 and are unchanged.

### Existing proxy security evidence (unchanged)

Exact route/method allowlist; identifier validation with a single re-encoding;
`after`/`limit` allowlisting on the employee list only; request bodies accepted
only for `POST /employees` and `PATCH /employees/{id}` with a 4 KiB cap; redirect
refusal; sanitized upstream failures; `no-store`; server-only `BACKEND_BASE_URL`;
no CORS headers; development-only fixture isolation. These are the controls
already covered by `frontend/tests/proxy.test.ts`; P4.8 added no security
guarantee and relaxed none.

**Phase 4 remains incomplete:** P4.9 (final verification, documentation and
acceptance review) is not started. Every previously recorded limitation still
stands — no authentication/RBAC, loopback-only backend, no multi-process JSONL
journal locking, no residual-session reconciliation, client-side attendance
filtering and paging, manual `PERSISTENCE_UNCONFIRMED` verification, and no live
PostgreSQL, real-camera/biometric, real-browser or deployment validation.
