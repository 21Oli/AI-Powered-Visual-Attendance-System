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
| P4.5 | Employees screen (list, create, detail, rename, deactivate) | not started |
| P4.6 | Attendance screen (journal, checkout confirmation) | not started |
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

## 5. Remaining work and constraints

* Employees, Attendance and System Status remain placeholders (P4.5–P4.7); the
  proxy already supports the write routes, but no write UI exists.
* **No live verification.** The backend's G2+G8 startup gate requires
  PostgreSQL; all proxy/Overview evidence is from mocked responses and
  development fixtures. No browser was driven either.
* Attendance has no server pagination and events carry no employee name; a future
  attendance list must join names from `GET /employees`.
* Checkout accepts no body and enforces the approved 300-second cooldown, which
  must be surfaced verbatim.
* An identifier containing `%` is refused by the proxy by design; ids are capped
  at 128 characters.
* No authentication or RBAC until Phase 5.
