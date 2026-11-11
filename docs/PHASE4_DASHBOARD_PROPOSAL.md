# Phase 4 — Web dashboard: proposed scaffold (awaiting dependency approval)

Status: **PROPOSAL ONLY — nothing installed, nothing created.**
`package.json`, `node_modules`, a lockfile and any frontend source file are
deliberately absent from the repository until the owner approves the dependency
list below.

---

## 1. Inspection result

| Checked | Result |
|---|---|
| `package.json` / `package-lock.json` / `pnpm-lock.yaml` / `yarn.lock` | none in the repository |
| `node_modules/` | absent |
| `next.config.*`, `vite.config.*`, `tsconfig.json` | absent |
| `pages/`, `app/`, `components/`, `public/` | absent (no frontend directory of any kind) |
| `src/` | Python only (Phase 1–3) |
| `requirements.txt` | Python only; no JS toolchain |
| Installed runtimes | not verified — no Node/npm version is claimed here |

**Conclusion: no frontend exists.** Phase 4 therefore starts from a scaffold, and
the choices below are recommendations, not a migration of existing code.

---

## 2. Recommended stack

| Choice | Why | Alternative rejected because |
|---|---|---|
| **Next.js (App Router)** | Route Handlers give a same-origin server-side proxy, so the browser never talks to FastAPI cross-origin and FastAPI keeps CORS disabled | Vite SPA needs a dev proxy plus a separate production proxy; CRA is deprecated |
| **TypeScript** | The API contract is small and fixed; types make "unsupported field" a compile error | plain JS cannot enforce the eight-endpoint contract |
| **CSS Modules + CSS custom properties** | Scoped styles, zero runtime, no design-system dependency | Tailwind adds a build plugin and a config surface for a four-screen dashboard |
| **React Server Components where they help** | The proxy already runs server-side; server rendering gives correct loading/empty/error markup | client-only data fetching duplicates logic |
| **No state library** | Four screens, one resource per screen | Redux/Zustand is unjustified at this size |

Recommended runtime floor: **Node 20 LTS** (Next 15 requires ≥18.18; 20 LTS is the
maintained choice). This must be confirmed against the owner's installed runtime
before installation.

---

## 3. Dependency list (exact, for approval)

`package.json` — proposed:

```json
{
  "name": "attendance-dashboard",
  "private": true,
  "version": "0.0.0",
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "lint": "next lint",
    "typecheck": "tsc --noEmit",
    "test": "node --test"
  },
  "dependencies": {
    "next": "15.5.4",
    "react": "19.1.1",
    "react-dom": "19.1.1"
  },
  "devDependencies": {
    "typescript": "5.9.2",
    "@types/node": "22.15.3",
    "@types/react": "19.1.9",
    "@types/react-dom": "19.1.7"
  }
}
```

Notes for the approval decision:

* **No test framework dependency is proposed.** `node --test` (built into Node 20)
  covers the pure API-client and formatting units; component rendering tests
  would need `@testing-library/react` + a DOM environment
  (`jsdom`/`happy-dom`) + a runner integration. That is a third dependency
  decision, so it is listed separately rather than smuggled in:
  `vitest`, `@vitejs/plugin-react`, `jsdom`, `@testing-library/react`,
  `@testing-library/user-event` — **not requested by default**.
* **No CSS framework, no icon package, no component library, no analytics, no
  auth package.** Phase 5 owns authentication.
* Versions are pinned exactly (no `^`), matching the Python side's pinning
  discipline; they must be re-checked at install time.

---

## 4. Proposed file layout (not created)

```text
dashboard/
  package.json
  tsconfig.json
  next.config.ts
  app/
    layout.tsx                 # shell, skip-link, nav
    globals.css                # CSS custom properties: colour, spacing, focus ring
    page.tsx                   # Overview
    employees/
      page.tsx                 # list + pagination + create
      [employeeId]/page.tsx    # detail + edit name + deactivate
    attendance/
      page.tsx                 # journal records + checkout confirmation
    status/
      page.tsx                 # API health / backend availability
    api/
      backend/
        [...path]/route.ts     # same-origin proxy → FastAPI (server-only)
  lib/
    api/
      client.ts                # typed fetch wrapper (server-side only)
      types.ts                 # the eight-endpoint response/error types
      errors.ts                # typed ApiError with code + status
    format.ts                  # timestamp/identifier formatting (pure)
  components/
    *.module.css, *.tsx        # Table, Pagination, StateBlock (loading/empty/error), ConfirmDialog
  test/
    *.test.ts                  # node --test units
```

---

## 5. Why a proxy, and exactly what it does

```text
browser ──same-origin──▶ Next Route Handler ──server-side fetch──▶ FastAPI (127.0.0.1:8000)
```

* FastAPI keeps **CORS disabled** and stays loopback-only; no CORS relaxation is
  introduced in development or production.
* The browser never receives `ATTENDANCE_DATABASE_URL`, a token, or any backend
  environment value. The proxy reads the backend base URL from a **server-only**
  environment variable (`BACKEND_BASE_URL`) that is never exposed through a
  `NEXT_PUBLIC_` prefix.
* The proxy forwards only the eight approved paths and methods — an allow-list,
  not a catch-all — and passes the upstream status and JSON body through
  unchanged, so error states are real rather than re-invented.
* No credential, DSN or stack trace is copied from the backend response.

---

## 6. Screens and their real data sources

| Screen | Source | States covered |
|---|---|---|
| **Overview** | `GET /api/v1/health`, `GET /api/v1/employees?limit=1`, `GET /api/v1/attendance` | loading, empty (no employees / no records), error (503 `ATTENDANCE_UNAVAILABLE`, `DATABASE_UNAVAILABLE`), offline |
| **Employees** | `GET /api/v1/employees` (`after`/`limit` keyset), `POST /api/v1/employees`, `GET/PATCH /api/v1/employees/{id}`, `POST /api/v1/employees/{id}/deactivate` | loading, empty, 409 `EMPLOYEE_EXISTS`, 404, 422 `EMPLOYEE_INVALID` |
| **Employee detail** | `GET /api/v1/employees/{id}` | loading, 404, error |
| **Attendance** | `GET /api/v1/attendance`, `POST /api/v1/attendance/{id}/checkout` | loading, empty, `incomplete_trailing_record: true`, 503 `ATTENDANCE_UNAVAILABLE`, checkout 409 `EMPLOYEE_INACTIVE` / `NO_OPEN_SESSION`, 503 `PERSISTENCE_UNCONFIRMED` |
| **System status** | `GET /api/v1/health` | reachable / unreachable |

Deliberate constraints:

* **No invented metrics.** No "attendance rate", no headcount trend, no
  percentage — the API exposes neither attendance totals nor sessions, only journal
  events. The dashboard presents exactly what the responses contain.
* **No manual check-in.** No approved endpoint performs a check-in (check-in is
  camera-authorized), so no check-in control is offered.
* **Checkout is operator-authorized** and is presented as such, behind a
  confirmation dialog, and it never claims biometric verification.
* **No camera streaming, no face images, no embeddings, no biometrics.**
* Any development fixture is labelled `FIXTURE` in the UI and is never presented
  as live data; with no fixture enabled every screen shows its real empty/error
  state.

---

## 7. Testing plan (existing tooling only, after approval)

| Area | Check |
|---|---|
| Navigation | every route reachable; unknown route → not-found |
| Responsive layout | 360 px, 768 px, 1280 px; no horizontal overflow |
| Loading / empty / error | one `StateBlock` per screen, each reachable via the injected client |
| API errors | typed `ApiError` mapping per documented code |
| Employee flows | create, edit name, deactivate (409/404/422 paths) |
| Attendance | record rendering, incomplete-tail notice, checkout confirm + rejection |
| Secrets | proxy modules never imported into client bundles; no `NEXT_PUBLIC_` DSN |
| Endpoint discipline | client exposes exactly the eight approved calls |

Component/interaction tests require the additional dependency decision in §3 and
are therefore **out of scope until approved**.

---

## 8. What is still blocked

| Item | Status |
|---|---|
| Dependency installation | **BLOCKED — awaiting owner approval** |
| Implementation of the scaffold | **NOT STARTED** (deliberately, per instruction) |
| Live backend integration | **NOT VERIFIED** — needs a running FastAPI process and a database, which this task may not start or reach |
| Authentication / RBAC | Phase 5 |
| Deployment | not requested; nothing may be published |

The backend has no authentication in Phase 3 by design (loopback-only, no CORS).
A dashboard proxy does not change that; anyone who can reach the loopback port can
use the API. Phase 5 must add authentication before any remote access.
