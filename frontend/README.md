# Frontend — Phase 4 dashboard

Next.js 16 (App Router) + TypeScript + CSS Modules dashboard for the
AI-Powered Visual Attendance System.

**Status: P4.5 complete — the Employees screen manages the register; the typed
API client, secure proxy, fixtures and the functional Overview are unchanged.**
Attendance and System Status remain structural placeholders (P4.6–P4.7).
No live backend has been verified: the FastAPI startup gate requires PostgreSQL,
which this phase is not authorized to connect to.

---

## Requirements

| Tool | Version used |
|---|---|
| Node.js | v24.21.0 (minimum declared: `>=20.9.0`) |
| npm | 11.19.0 |

## Install and run

```bash
cd frontend
npm ci            # reproducible install from package-lock.json
npm run dev       # http://localhost:3000 → redirects to /overview
```

### Backend configuration (server-only)

The Overview reads through the same-origin proxy, which needs one **server-side**
variable:

```bash
# frontend/.env.local  (git-ignored — never commit it)
BACKEND_BASE_URL=http://127.0.0.1:8000
```

Rules the code enforces:

* `BACKEND_BASE_URL` is read only by the Route Handler. There is no
  `NEXT_PUBLIC_BACKEND_BASE_URL` anywhere, and the browser never receives the
  value.
* It must be an absolute `http(s)` **origin**: credentials, query, fragment and
  path are refused, and an invalid or missing value produces a sanitized
  `503 BACKEND_NOT_CONFIGURED` / `BACKEND_CONFIGURATION_INVALID` instead of a
  guessed destination.
* Without it, the Overview shows a real unavailable state — never fixture data.

### Development fixtures (optional)

Offline UI work uses synthetic data that can only be enabled on the server in a
development build:

```bash
# frontend/.env.local
DASHBOARD_DATA_MODE=fixtures
```

* Requires **both** `NODE_ENV === "development"` and the exact
  `DASHBOARD_DATA_MODE=fixtures` value; in a production build fixture mode is
  impossible whatever the variable says.
* The Overview shows a persistent `FIXTURE DATA — DEVELOPMENT ONLY` banner, and
  the data source kind is passed from the server so the UI can label it.
* There is **no** fallback path from a failed live request to fixtures: a failing
  backend yields an error state.
* Remove the variable (or set it to anything else) to return to the live proxy.

## Scripts

| Script | Command | Purpose |
|---|---|---|
| `dev` | `next dev` | development server |
| `build` | `next build` | production build |
| `start` | `next start` | serve the production build |
| `lint` | `eslint .` | ESLint flat config |
| `typecheck` | `tsc --noEmit` | strict TypeScript |
| `test` | `vitest run` | unit, proxy and component tests |
| `test:watch` | `vitest` | watch mode |

## Routes

| Route | Area | State |
|---|---|---|
| `/` | — | redirects to `/overview` |
| `/overview` | Overview | **functional** (dynamic) |
| `/employees` | Employees | **functional** (client component) |
| `/attendance` | Attendance | structural placeholder |
| `/system-status` | System Status | structural placeholder |
| `/api/backend/[...path]` | — | **proxy** (dynamic, server-only) |
| any other path | — | `not-found` |

## API architecture

```text
Browser
  │  same-origin fetch, no backend origin, no credentials in the bundle
  ▼
OverviewDashboard (client component)
  ▼
src/lib/api/client.ts        typed JSON transport, response validation
  ▼
/api/backend/[...path]       Route Handler: allowlist + policy
  ▼
BACKEND_BASE_URL             server-only env
  ▼
FastAPI (loopback)  →  PostgreSQL / JSONL journal
```

| Module | Responsibility |
|---|---|
| `src/lib/api/types.ts` | response interfaces + `parse*` runtime validators |
| `src/lib/api/errors.ts` | `ApiError` (`network` / `http` / `malformed` / `unknown`) |
| `src/lib/api/client.ts` | single URL builder, fetch, JSON + envelope parsing |
| `src/lib/api/employees.ts` | keyset paging, full-collection walk |
| `src/lib/api/attendance.ts` | the whole journal (the API has no paging) |
| `src/lib/api/data-source.ts` | live vs fixture source, resolved server-side |
| `src/lib/dashboard/overview.ts` | pure metric calculations |
| `src/lib/server/proxy.ts` | allowlist, id/query validation, upstream URL |
| `src/lib/employees/validation.ts` | employee-ID rule (delegates to the proxy's `isSafeEmployeeId`) and name rule |
| `src/lib/employees/format.ts` | UTC calendar date and status labels |
| `src/lib/employees/errors.ts` | API failure → employee form field mapping |
| `src/components/employees/` | directory, pagination, create form, detail/rename |
| `src/components/ui/` | `TextField`, `ConfirmDialog`, `StatusMessage` |
| `src/app/api/backend/[...path]/route.ts` | thin Route Handler adapter |

### Proxy allowlist

Exactly the eight published method/path pairs — nothing else is reachable:

```
GET   /api/v1/health
POST  /api/v1/employees
GET   /api/v1/employees
GET   /api/v1/employees/{employee_id}
PATCH /api/v1/employees/{employee_id}
POST  /api/v1/employees/{employee_id}/deactivate
GET   /api/v1/attendance
POST  /api/v1/attendance/{employee_id}/checkout
```

Security behaviour:

* **No client-chosen destination.** The upstream URL is rebuilt from the matched
  route plus the validated base origin, so a query or header cannot redirect it.
* **Identifier safety.** A segment must be non-empty, ≤128 chars, trimmed, not
  `.`/`..`, and free of `/`, `\`, `?`, `#`, `%` and control characters; it is
  re-encoded exactly once when forwarded.
* **Query policy.** Only `after` and `limit` are accepted, and only on the
  employee list; `limit` must be a positive integer ≤ 4 digits. Every other
  route rejects any query parameter.
* **Body policy.** A body is forwarded only for `POST /employees` and
  `PATCH /employees/{id}`, capped at 4 KiB. Checkout never gets a body, even if
  one is supplied.
* **Redirects are never followed** (`redirect: "manual"`); a 3xx becomes
  `502 BACKEND_REDIRECT_REFUSED`, so traffic cannot leave the trusted origin.
* **Failures are sanitized**: `502 BACKEND_UNAVAILABLE`, `504 BACKEND_TIMEOUT`,
  `502 BACKEND_EMPTY_RESPONSE`, `500 PROXY_INTERNAL_ERROR` — no upstream URL,
  stack trace, driver message or payload is returned or logged.
* **No CORS headers** are added and no data is cached (`cache-control: no-store`).

## Overview metric definitions

| Metric | Derivation |
|---|---|
| **Total employees** | Every keyset page is fetched (`limit=100`, following `next_after` until `null`) and the records counted. The response `count` is only the page length, so it is never treated as a total. |
| **Active employees** | The same records, counted where `is_active === true`. Nothing else implies activity. |
| **Events today (UTC)** | Journal events whose `occurred_at` falls on the current **UTC** calendar day. Labelled `(UTC)` because it is not the viewer's local day. |
| **Open sessions** | The journal is folded to one latest event per employee by `(occurred_at, journal position)`; an employee counts when that event is `check_in`. Ties go to the later journal position, matching the backend's own rule. |
| **Journal integrity** | `incomplete_trailing_record` from the attendance response, surfaced as a warning. The journal is never repaired or rewritten. |

Deliberately absent: attendance rate, trends, accuracy, productivity scores,
predictions, rankings, charts. The API exposes journal events and employee
records; nothing else is invented.

Pagination safeguards: a repeated cursor throws `PAGINATION_CURSOR_REPEATED` and
an over-long walk throws `PAGINATION_LIMIT_REACHED`, so a partial collection is
never displayed as a total. An unreadable page fails the whole metric.

## States

* **Loading** — each card shows `Loading…`; a `role="status"` region announces it.
* **Empty** — real zeroes with an explanation ("No employees are registered yet").
* **Error** — a labelled panel with the sanitized message, the API code and a
  keyboard-operable **Retry**; the header also offers **Refresh**.
* **Partial failure** — employees and attendance load independently, so one
  failing section shows `Unavailable` while the other still reports real values.
* **Fixture** — persistent `FIXTURE DATA — DEVELOPMENT ONLY` banner.

## Structure

```text
frontend/src/
├── app/
│   ├── (dashboard)/{layout.tsx, overview/, employees/, attendance/, system-status/}
│   ├── api/backend/[...path]/route.ts        # proxy
│   ├── layout.tsx, page.tsx, loading.tsx, error.tsx, not-found.tsx, globals.css
├── components/
│   ├── dashboard/{OverviewDashboard,MetricCard} + CSS Modules
│   ├── layout/{AppShell,Sidebar,Header,MobileNavigation,PagePlaceholder}
│   ├── icons/NavIcon.tsx
│   └── ui/{Button,ThemeToggle}
├── hooks/useTheme.ts
├── lib/
│   ├── api/{types,errors,client,employees,attendance,data-source}.ts
│   ├── dashboard/overview.ts
│   ├── fixtures/overview-fixtures.ts
│   ├── server/proxy.ts
│   └── navigation.ts, theme.ts, themeStore.ts
└── tests/
```

## Employees screen (P4.5)

`/employees` is a client component that talks only to the same-origin proxy.

| Action | Request | Notes |
|---|---|---|
| List | `GET /api/v1/employees?limit=&after=` | keyset only; `limit` 1–100 |
| Create | `POST /api/v1/employees` | `employee_id`, `full_name`, `is_active: true`; never `created_at` |
| Detail | `GET /api/v1/employees/{employee_id}` | |
| Rename | `PATCH /api/v1/employees/{employee_id}` | `full_name` only |
| Deactivate | `POST /api/v1/employees/{employee_id}/deactivate` | no body; no reactivation endpoint exists |

**Employee-ID compatibility (owner-approved Option A).** The create form
validates `employee_id` with the proxy's own `isSafeEmployeeId`, imported rather
than copied, so the dashboard can never create an employee it cannot then
address. The value is submitted exactly as typed — no trimming, case folding or
other normalisation — and valid leading zeros, hyphens and mixed case are kept.
Records that already exist in the backend but violate the proxy policy stay
listed, with detail/rename/deactivate disabled and the reason shown; such an
operation is never attempted and never reported as successful.

**Keyset pagination.** `Next` follows `next_after`; `Previous` replays a
client-side cursor history, so nothing is re-scanned. Page sizes are 10/20/50/100
(the backend maximum is 100) and changing the size returns to the first page. A
full final page can be followed by a legitimate empty page, which is shown as
such with a button back. `count` is the page length and is never presented as a
total; there is no total, no search and no sorting, and no snapshot consistency
is claimed. Requests are abortable and tagged, so a stale response cannot replace
a newer page.

**Deactivation** sits behind a confirmation dialog that names the employee,
states that it cannot be undone through the dashboard (the API has no
reactivation endpoint), moves focus to Cancel, traps Tab, closes on Escape and
restores focus to the invoking button. Success appears only after the API
returns; the directory is then refetched and the row's status updated.

**Fixture limitation.** P4.5 adds no employee fixtures: the development fixture
gate still covers the Overview only, so a failing live employee request always
produces a distinguishable error state instead of synthetic records.

## Dependencies (exact pins)

Production: `next 16.4.6`, `react 19.3.1`, `react-dom 19.3.1`.

Development: `typescript 5.9.3`, `@types/node 24.19.2`, `@types/react 19.3.0`,
`@types/react-dom 19.3.0`, `eslint 10.14.0`, `eslint-config-next 16.4.6`,
`vitest 5.1.0`, `vite 8.3.8`, `jsdom 31.0.0`, `@testing-library/react 16.3.3`,
`@testing-library/dom 10.4.2`, `@testing-library/user-event 14.6.9`.

P4.4 added **no** dependency: no data-fetching, validation, charting, icon or
state library.

## Tests

```bash
npm run test
```

**249 tests in 19 suites** (P4.4: 184).

| Suite | Covers |
|---|---|
| `proxy.test.ts` | all eight pairs forwarded, unknown path/method refused, traversal and padded ids refused, query policy, body policy, status/envelope preservation, no redirect, 502/504 sanitized, config validation, no origin leak |
| `api-client.test.ts` | same-origin URLs, methods, envelope mapping (404/409/422/503), malformed JSON, wrong shape, network vs abort |
| `fixtures.test.ts` | off by default, dev+opt-in only, impossible in production, label, contract-shaped data, determinism, no silent fallback |
| `overview-metrics.test.ts` | multi-page totals, cursor loops, page failure, UTC boundaries, folding, ties, duplicates, undated/unclassified, integrity flag |
| `overview.test.tsx` | loading, live success, empty, fixture banner, integrity warning, independent failures, retry, keyboard refresh, no fabricated metrics, light theme |
| `employee-id.test.ts` | id rule: ordinary ids, leading zeros/hyphens/mixed case, empty, padded, >128 chars, `/ \\ % # ?`, `.`/`..`, control characters; date and status formatting |
| `employees-errors.test.ts` | `EMPLOYEE_EXISTS`, `body.`-prefixed field mapping, unmapped fields preserved, proxy 413, 503, network, rename 404 |
| `employees-api.test.ts` | create/detail/rename/deactivate methods, paths, bodies, parsing, error propagation |
| `employees-directory.test.tsx` | loading, empty, populated, unsupported ids, error/retry, next/previous, page size, empty final page, no total, stale responses |
| `employees-create.test.tsx` | required fields, unsupported id, success body, 409, 422 mapping, unmapped 422, proxy 413, network, cancel |
| `employees-rename.test.tsx` | detail load, whitespace name, rename body, 422/404/503, deactivate hand-off |
| `employees-deactivate.test.tsx` | confirmation required, cancel, Escape with focus restore, single POST, refresh, failure |
| `routes.test.tsx` | root redirect, functional Overview and Employees routes, remaining placeholders |
| theme / tokens / navigation / shell suites | unchanged from P4.2–P4.3 |

Measured on 2026-11-12 (Node v24.21.0, npm 11.19.0):

| Check | Result |
|---|---|
| `npm ls --depth=0` | exit 0 — 15 packages, unchanged |
| `npm run lint` | exit 0, 0 problems |
| `npm run typecheck` | exit 0 |
| `npm run test` | exit 0 — **249 passed / 249**, 19 suites, ~49 s |
| `npm run build` | exit 0 — static `/`, `/_not-found`, `/attendance`, `/employees`, `/system-status`; dynamic `/overview` and `/api/backend/[...path]` |

## Known limitations

* **No live integration has been verified.** The backend's G2+G8 startup gate
  requires PostgreSQL, so every Proxy and Overview check above uses mocked
  responses or development fixtures.
* No browser was driven; responsive layout and the proxy's real network path are
  covered by tests and static review only.
* Attendance and System Status are still placeholders; there is no checkout
  control or health widget.
* No employee fixtures exist, so the Employees screen has no offline mode: with
  no backend it shows its error state.
* Attendance is not paginated and events carry no employee name — a future
  activity list must join names from the employee endpoint.
* An employee identifier containing `%` cannot pass the proxy (documented
  defence against ambiguous encoding); ids are also limited to 128 characters.
* No authentication or RBAC (Phase 5); the backend stays loopback-only.

## Safety

No credential, DSN, token or secret is present in this app, and no secret is ever
sent to the browser. No camera, photograph, embedding or biometric data is
accessed.
