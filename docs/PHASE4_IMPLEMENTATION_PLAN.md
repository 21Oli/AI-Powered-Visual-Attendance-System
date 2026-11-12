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
| P4.4 | Typed API client, secure proxy, fixtures and Overview | **READY FOR OWNER REVIEW** |
| P4.5 | Employees screen (list, create, detail, rename, deactivate) | **READY FOR OWNER REVIEW** |
| P4.6 | Attendance screen (journal, checkout confirmation) | journal **READY FOR OWNER REVIEW** (P4.6.1–P4.6.4); checkout UI not started |
| P4.7 | System status screen (API health, offline states) | not started |
| P4.8 | Remaining API surfaces (writes) and hardening | not started |
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

**Read-only by construction.** The page has no checkout control and never
imports `checkoutEmployee`, so no attendance mutation can be triggered from it;
the checkout client and its error classifier from P4.6.3 stay unused by the UI.

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
| `npm run test` | exit 0 — **398 passed / 398**, 27 suites |
| `npm run build` | exit 0 — dynamic `/attendance`; static `/employees`, `/system-status` |

Test growth 352 → 398 (+46) across three new attendance suites and the updated
route suite. The only first-run failure that needed a source change (rather than
a test query) was the pre-existing fixture determinism test, which correctly
requires two different clocks to produce different data; a record is now placed
at the fixture clock instant instead of weakening that assertion.

---

## 6. Remaining work and constraints

* System Status remains a placeholder (P4.7). The Attendance journal is
  implemented read-only (P4.6.4) and there is still no checkout control, so
  `checkoutEmployee` stays unused by the UI.
* **No live verification.** The backend's G2+G8 startup gate requires
  PostgreSQL; all proxy/Overview evidence is from mocked responses and
  development fixtures. No browser was driven either.
* Attendance has no server pagination, so the screen downloads the whole journal
  and pages it in the browser; events carry no employee name, so names come from
  walking every `GET /employees` page and are never inferred.
* Checkout accepts no body and enforces the approved 300-second cooldown, which
  must be surfaced verbatim.
* An identifier containing `%`, `#`, `?`, `/` or `\` is refused by the proxy by
  design; ids are capped at 128 characters and may not have surrounding
  whitespace. Such records can be listed but not managed, which is displayed
  rather than hidden.
* The Employees screen has no fixture mode: the development fixture gate covers
  the Overview only.
* No authentication or RBAC until Phase 5.
