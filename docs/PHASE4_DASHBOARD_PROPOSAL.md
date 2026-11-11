# Phase 4 — Web dashboard: approved direction

Status: **APPROVED and in progress.** The owner approved the stack and the exact
dependency list; P4.2 created the scaffold under `frontend/`.

This document records the architecture the owner approved. Milestone-by-milestone
status and verification results live in `docs/PHASE4_IMPLEMENTATION_PLAN.md`.

---

## 1. Inspection result (P4.1, unchanged)

| Checked | Result |
|---|---|
| `package.json` / lockfiles / `node_modules` | none existed before P4.2 |
| `next.config.*`, `tsconfig.json`, `app/`, `components/`, `public/` | none existed before P4.2 |
| `src/` | Python only (Phase 1–3) |
| Installed runtimes | **verified**: Node v24.21.0, npm 11.19.0 |

No frontend existed, so Phase 4 starts from a scaffold rather than migrating
code.

---

## 2. Approved stack

| Choice | Why | Rejected alternative |
|---|---|---|
| **Next.js 16 (App Router)** | Route Handlers give a same-origin server-side proxy, so the browser never talks to FastAPI cross-origin and FastAPI keeps CORS disabled | a Vite SPA needs a dev proxy *and* a separate production proxy |
| **TypeScript 5.9 (strict)** | the API contract is small and fixed; types make an unsupported field a compile error | plain JS cannot enforce the eight-endpoint contract |
| **CSS Modules + custom properties** | scoped styles, zero runtime, no design-system dependency | Tailwind adds a build plugin and config surface for four screens |
| **No state library** | one resource per screen | Redux/Zustand is unjustified at this size |
| **No UI framework, no icon package** | tokens plus two primitives cover the design | adds dependency surface for no benefit |

Runtime: Node **v24.21.0** (declared floor `>=20.9.0`).

---

## 3. Dependencies — approved and installed

Production: `next 16.4.6`, `react 19.3.1`, `react-dom 19.3.1`.

Development: `typescript 5.9.3`, `@types/node 24.19.2`, `@types/react 19.3.0`,
`@types/react-dom 19.3.0`, `eslint 10.14.0`, `eslint-config-next 16.4.6`,
`vitest 5.1.0`, `vite 8.3.8`, `jsdom 31.0.0`, `@testing-library/react 16.3.3`,
`@testing-library/dom 10.4.2`, `@testing-library/user-event 14.6.9`.

All versions are pinned exactly and locked in `frontend/package-lock.json`.

Corrections to this document's earlier draft, now applied:

* Node 20 LTS → **Node 24.21.0 verified**.
* `next 15.5.4` / `react 19.1.1` → **`next 16.4.6` / `react 19.3.1`**.
* `next lint` → **removed in Next 16**; the lint script is `eslint .` with the
  flat config that `eslint-config-next` 16 exports directly.
* `node --test` with no DOM → the owner approved **Vitest + jsdom +
  Testing Library**, because component and interaction tests are required.
* Layout `dashboard/` at repository root → **`frontend/` with `src/app/`**.

---

## 4. Layout

Created in P4.2:

```text
frontend/
  src/app/{layout,page,globals.css,loading,error}
  src/components/ui/{Button,ThemeToggle}
  src/hooks/useTheme.ts
  src/lib/{theme.ts,themeStore.ts}
  src/{styles,types}/          # reserved
  tests/
  package.json, package-lock.json, tsconfig.json, next.config.ts,
  eslint.config.mjs, vitest.config.mts, README.md
```

Planned, not yet created: `src/app/(dashboard)/{overview,employees,attendance,system}/`
and `src/app/api/backend/[...path]/route.ts`.

---

## 5. Why a proxy, and exactly what it does

```text
browser ──same-origin──▶ Next Route Handler ──server-side fetch──▶ FastAPI (127.0.0.1:8000)
```

* FastAPI keeps **CORS disabled** and stays loopback-only; no CORS relaxation is
  introduced in development or production.
* The browser never receives `ATTENDANCE_DATABASE_URL`, a token, or any backend
  environment value. The proxy reads the backend base URL from a **server-only**
  variable (`BACKEND_BASE_URL`), never a `NEXT_PUBLIC_` one.
* The proxy forwards only the eight approved path+method pairs — an allow-list,
  not a catch-all — and passes the upstream status and JSON body through
  unchanged, so error states are real rather than re-invented.
* No credential, DSN or stack trace is copied from a backend response.

---

## 6. Screens and their real data sources

| Screen | Source | States covered |
|---|---|---|
| **Overview** | `GET /api/v1/health`, `GET /api/v1/employees?limit=1`, `GET /api/v1/attendance` | loading, empty, 503 `ATTENDANCE_UNAVAILABLE` / `DATABASE_UNAVAILABLE`, offline |
| **Employees** | `GET/POST /api/v1/employees`, `GET/PATCH /api/v1/employees/{id}`, `POST …/deactivate` | loading, empty, 409 `EMPLOYEE_EXISTS`, 404, 422 `EMPLOYEE_INVALID` + `fields` |
| **Attendance** | `GET /api/v1/attendance`, `POST /api/v1/attendance/{id}/checkout` | loading, empty, `incomplete_trailing_record`, 409 with `EMPLOYEE_INACTIVE` / `NO_OPEN_SESSION` / `CHECKOUT_COOLDOWN`, 503 `PERSISTENCE_UNCONFIRMED` |
| **System status** | `GET /api/v1/health` | reachable / unreachable |

Verified contract facts that shape the UI:

* `GET /employees` is keyset-paginated (`after`, `limit` default 20, max 100,
  `next_after`), and `limit < 1` is `422 EMPLOYEE_INVALID`.
* **Attendance has no server pagination** — one response carries the whole
  validated journal, so any paging is client-side and labelled as such.
* **Events carry no employee name** — only `employee_id`; names come from the
  employees endpoint and are never invented.
* `POST …/checkout` accepts **no body** (`extra: forbid`), and the
  `CHECKOUT_COOLDOWN` reason reflects the approved 300-second cooldown.

Deliberate constraints:

* **No invented metrics** — no attendance rate, no headcount trend, no
  percentage; the API exposes neither totals nor sessions, only journal events.
* **No manual check-in** — no approved endpoint performs one (check-in is
  camera-authorized).
* **Checkout is operator-authorized**, presented as such behind a confirmation
  dialog, and never claims biometric verification.
* **No camera streaming, no face images, no embeddings, no biometrics.**

---

## 7. Testing plan

| Area | Check |
|---|---|
| Design system | every colour token in both themes, focus ring, reduced motion |
| Theme | precedence, persistence, toggle, keyboard, no flash, SSR safety |
| Navigation | every route reachable; unknown route → not-found (P4.3+) |
| Responsive layout | 360 / 768 / 1280 px, no horizontal overflow (P4.3+) |
| Loading / empty / error | each state reachable per screen (P4.4+) |
| API errors | typed mapping per documented code (P4.8) |
| Employee flows | create, rename, deactivate, 409/404/422 paths (P4.5) |
| Attendance | record rendering, incomplete-tail notice, checkout confirm + rejection (P4.6) |
| Secrets | proxy modules never imported into client bundles; no `NEXT_PUBLIC_` secret |

P4.2 delivered the design-system and theme rows: **56 tests, all passing**.

---

## 8. Fixture policy

Development-only fixtures are approved for later offline milestones:

* never selectable silently in a production build;
* labelled `FIXTURE DATA` in the UI wherever they appear;
* never mixed with a real backend response in the same view;
* never written to the backend, database or attendance journal.

P4.2 creates **no fixture data** — none is needed for the scaffold.

---

## 9. Still blocked

| Item | Status |
|---|---|
| Live backend integration | **NOT VERIFIED** — the FastAPI startup gate (G2+G8) needs a PostgreSQL connection, which this phase may not open |
| Screens, navigation, proxy | not started (P4.3–P4.8) |
| Authentication / RBAC | Phase 5 |
| Deployment | not requested; nothing may be published |

The backend has no authentication in Phase 3 by design (loopback-only, no CORS).
A dashboard proxy does not change that; Phase 5 must add authentication before any
remote access.
