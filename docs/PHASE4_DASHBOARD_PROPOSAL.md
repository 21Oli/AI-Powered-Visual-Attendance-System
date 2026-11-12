# Phase 4 — Web dashboard: approved direction

Status: **APPROVED; P4.1–P4.6.5 implemented and committed.** P4.2 created the
scaffold, P4.3 the shell and navigation, P4.4 the typed API client, the secure
proxy, development fixtures and the functional Overview, P4.5 the Employees
screen, P4.6 the Attendance journal with client-side filters/paging and the
confirmed operator check-out, and P4.7 the backend-API liveness screen. P4.6.6
re-verified the committed code; P4.8–P4.9 are not started.

Milestone status and verification live in
`docs/PHASE4_IMPLEMENTATION_PLAN.md`; operational detail in
`frontend/README.md`.

---

## 1. Approved stack

| Choice | Why | Rejected alternative |
|---|---|---|
| **Next.js 16 (App Router)** | Route Handlers give a same-origin server-side proxy, so the browser never talks to FastAPI cross-origin and FastAPI keeps CORS disabled | a Vite SPA needs a dev proxy *and* a separate production proxy |
| **TypeScript 5.9 (strict)** | the API contract is small and fixed; types make an unsupported field a compile error | plain JS cannot enforce the eight-endpoint contract |
| **CSS Modules + custom properties** | scoped styles, zero runtime, no design-system dependency | Tailwind adds a build plugin and config surface for four screens |
| **No state or data-fetching library** | two read-only resources on one screen | React Query/Zustand is unjustified at this size |
| **No UI framework, no icon package, no chart library** | tokens plus two primitives cover the design | extra dependency surface for no benefit |

Runtime: Node **v24.21.0** (declared floor `>=20.9.0`).

---

## 2. Dependencies — approved and installed (unchanged through P4.4)

Production: `next 16.4.6`, `react 19.3.1`, `react-dom 19.3.1`.

Development: `typescript 5.9.3`, `@types/node 24.19.2`, `@types/react 19.3.0`,
`@types/react-dom 19.3.0`, `eslint 10.14.0`, `eslint-config-next 16.4.6`,
`vitest 5.1.0`, `vite 8.3.8`, `jsdom 31.0.0`, `@testing-library/react 16.3.3`,
`@testing-library/dom 10.4.2`, `@testing-library/user-event 14.6.9`.

All versions are pinned exactly and locked in `frontend/package-lock.json`.

---

## 3. Layout

```text
frontend/
  src/app/
    layout.tsx, page.tsx (→ /overview), globals.css, loading, error, not-found
    (dashboard)/{layout.tsx, overview/, employees/, attendance/, system-status/}
    api/backend/[...path]/route.ts        # proxy (implemented in P4.4)
  src/components/{dashboard,layout,ui,icons}/
  src/hooks/useTheme.ts
  src/lib/api/{types,errors,client,employees,attendance,data-source}.ts
  src/lib/dashboard/overview.ts
  src/lib/fixtures/overview-fixtures.ts
  src/lib/server/proxy.ts
  src/lib/{navigation,theme,themeStore}.ts
  tests/
```

`Employees` (P4.5) and the read-only `Attendance` journal (P4.6.4) are
implemented; `System Status` is still a structural placeholder.

---

## 4. Why a proxy, and exactly what it does

```text
browser ──same-origin──▶ Next Route Handler ──server-side fetch──▶ FastAPI (127.0.0.1:8000)
```

* FastAPI keeps **CORS disabled** and stays loopback-only; no CORS relaxation is
  introduced in development or production.
* The browser never receives `ATTENDANCE_DATABASE_URL`, a token, or any backend
  environment value. The proxy reads the backend origin from the **server-only**
  `BACKEND_BASE_URL`; there is no `NEXT_PUBLIC_` variant.
* The proxy forwards only the eight approved path+method pairs — an allow-list,
  not a catch-all — and passes upstream status and JSON body through unchanged,
  so error states are real rather than re-invented.
* It refuses client-chosen destinations, follows no redirects, accepts only
  `after`/`limit` on the employee list, forwards a body only for the two write
  routes (≤4 KiB), and returns sanitized `502`/`504` failures.
* No credential, DSN or stack trace is copied from a backend response, and
  nothing is cached (`cache-control: no-store`).

---

## 5. Screens and their real data sources

| Screen | Source | State |
|---|---|---|
| **Overview** | `GET /api/v1/employees` (all keyset pages), `GET /api/v1/attendance` | **implemented** — loading, empty, error, partial, fixture |
| **Employees** | `GET/POST /api/v1/employees`, `GET/PATCH /api/v1/employees/{id}`, `POST …/deactivate` | **implemented** — loading, empty, error/retry, keyset paging, create, rename, confirmed deactivation |
| **Attendance** | `GET /api/v1/attendance`, `GET /api/v1/employees` (all keyset pages, for names and the employee filter), `POST /api/v1/attendance/{id}/checkout` | **implemented** — journal states as before, plus a confirmed operator check-out for an active employee with an open session in live mode (one request, no retry, no optimistic close, uncertain `503` reported as uncertain, fixture mode disabled) |
| **System Status** | `GET /api/v1/health` | **implemented** (P4.7) — liveness only: loading, healthy, unreachable, timeout, unexpected response, server error, manual refresh, fixture disclosure; no database/camera/recognition/deployment probe |

Verified contract facts that shape the UI:

* `GET /employees` is keyset-paginated (`after`, `limit` default 20, max 100,
  `next_after`); its `count` is the **page length**, not a total.
* **Attendance has no server pagination**; one response carries the whole
  validated journal, so the screen fetches it once and filters and pages in the
  browser. No attendance query parameters exist to implement server-side.
* **Events carry no employee name** — only `employee_id`; names must come from
  the employees endpoint and are never invented.
* Event types are lowercase `check_in` / `check_out`; timestamps are UTC ISO-8601;
  `created_at` is nullable.
* `POST …/checkout` accepts **no body**, and `CHECKOUT_COOLDOWN` reflects the
  approved 300-second cooldown.
* `GET /health` returns exactly `{ "status": "ok" }` and does **not** touch
  PostgreSQL: it is a liveness claim about the API process, and the backend's own
  docstring names `GET /employees` as the honest database probe. The status
  screen repeats that limit rather than implying system-wide health.

Deliberate constraints: no invented metrics (no rate, trend, score, prediction or
chart), no manual check-in, checkout presented as operator-authorized, and no
camera streaming, face images, embeddings or biometrics.

**Employee-ID compatibility (P4.5, owner-approved Option A).** The create form
validates with the proxy's own rule (`isSafeEmployeeId`, imported rather than
copied), so no employee can be created that the dashboard could not then read,
rename or deactivate. Ids are sent exactly as typed. Records that already violate
the proxy policy are listed, unmanageable, with the reason shown — never hidden
and never silently "fixed".

**Pagination and deactivation.** Keyset only: `Next` follows `next_after`,
`Previous` replays a client-side cursor history, page sizes are 10/20/50/100, and
`count` is never presented as a total. Deactivation is confirmed in a modal that
names the employee and states there is no reactivation endpoint; the status
changes only after the API returns.

---

## 6. Fixture policy

Development-only fixtures are implemented for the Overview and Attendance
screens only (the Employees screen has no fixture mode):

* enabled by `NODE_ENV=development` **and** `DASHBOARD_DATA_MODE=fixtures`,
  resolved on the server, impossible in a production build;
* always labelled `FIXTURE DATA — DEVELOPMENT ONLY` in the UI;
* never mixed with, or substituted for, a failed live response;
* synthetic only — no real employee identifiers, names or biometric data.

---

## 7. Testing

| Area | Check | State |
|---|---|---|
| Design system / theme | tokens in both themes, focus ring, reduced motion, persistence | covered (P4.2) |
| Shell / navigation | landmarks, active state, drawer, focus, keyboard | covered (P4.3) |
| Proxy | allowlist, id/query/body policy, status preservation, redirect refusal, sanitized failures | covered (P4.4) |
| API client | URLs, methods, envelopes, malformed bodies, network vs abort | covered (P4.4) |
| Fixtures | gating, production refusal, labelling, contract shape, no fallback | covered (P4.4) |
| Metrics | multi-page totals, cursor loops, UTC boundaries, folding, ties | covered (P4.4) |
| Overview UI | loading, empty, error, partial, retry, integrity warning | covered (P4.4) |
| Employees UI | id rule (proxy-delegated), keyset paging, empty final page, create errors, rename, deactivation dialog and focus | covered (P4.5) |
| Attendance / checkout | journal states, filters, paging, eligibility, single bodyless POST, duplicate prevention, success+refresh, every error classification, uncertainty without retry | covered (P4.6.2–P4.6.5) |
| System Status | health client (path, payload, malformed/HTTP/network/timeout/abort) and all screen states, refresh, duplicate prevention, fixture disclosure | covered (P4.7) |
| Navigation / responsive / write flows | route rendering, 360–1280 px, checkout | routes and all three writes covered by tests; **no real browser was driven**, so responsive and focus behaviour are static/CSS review only |

---

## 8. Still blocked

| Item | Status |
|---|---|
| Live backend integration | **NOT VERIFIED** — the FastAPI startup gate (G2+G8) needs PostgreSQL, which this phase may not open |
| Browser verification | not performed |
| Employee CRUD | implemented in P4.5 (list, create, rename, deactivate) |
| Attendance screen | journal + operator check-out implemented (P4.6.4–P4.6.5) |
| Status screen | backend API liveness screen implemented (P4.7); no database, camera, recognition or deployment probe exists |
| Phase 4 final review (P4.6.6) | automated frontend verification passed (439/439, lint, typecheck, build, 15 unchanged packages); live backend, real browser and deployment not verified |
| Authentication / RBAC | Phase 5 |
| Deployment | not requested; nothing may be published |

The backend has no authentication in Phase 3 by design (loopback-only, no CORS).
A dashboard proxy does not change that; Phase 5 must add authentication before any
remote access.
