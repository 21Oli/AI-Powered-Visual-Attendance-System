# Phase 4 — Implementation plan and milestone status

Owner-approved direction: Next.js App Router, TypeScript (strict), CSS Modules,
CSS custom properties, no Tailwind, no UI framework, dark graphite + teal with a
light theme, and a same-origin Route Handler proxy so FastAPI keeps CORS
disabled.

Phase 4 baseline: branch `main`, HEAD `55f0dae` (Phase 3 committed, clean tree).

---

## 1. Milestones

| # | Milestone | Status |
|---|---|---|
| P4.1 | Repository inspection, architecture and dependency approval list | **PASSED** (owner-approved) |
| P4.2 | Frontend scaffold and design system | **PASSED** (owner-approved, committed as `e3ead4d`) |
| P4.3 | Application shell and navigation | **READY FOR OWNER REVIEW** |
| P4.4 | Overview screen | not started |
| P4.5 | Employees screen (list, create, detail, rename, deactivate) | not started |
| P4.6 | Attendance screen (journal, checkout confirmation) | not started |
| P4.7 | System status screen (API health, offline states) | not started |
| P4.8 | Same-origin Route Handler proxy + live typed client | not started |
| P4.9 | Final verification, documentation and acceptance review | not started |

Every milestone stops for explicit owner approval; no milestone starts early.

---

## 2. Approved dependency set (unchanged since P4.2)

| Package | Version | Scope |
|---|---|---|
| `next` | 16.4.6 | production |
| `react` | 19.3.1 | production |
| `react-dom` | 19.3.1 | production |
| `typescript` | 5.9.3 | development |
| `@types/node` | 24.19.2 | development |
| `@types/react` | 19.3.0 | development |
| `@types/react-dom` | 19.3.0 | development |
| `eslint` | 10.14.0 | development |
| `eslint-config-next` | 16.4.6 | development |
| `vitest` | 5.1.0 | development |
| `vite` | 8.3.8 | development |
| `jsdom` | 31.0.0 | development |
| `@testing-library/react` | 16.3.3 | development |
| `@testing-library/dom` | 10.4.2 | development |
| `@testing-library/user-event` | 14.6.9 | development |

P4.3 installed, upgraded and removed **nothing**. Icon, drawer, routing and state
needs were met with inline SVG, a native dialog-lite pattern, `next/link` and
`useState`.

---

## 3. Delivered structure

P4.2 (committed):

```text
frontend/src/app/{layout,page,globals.css,loading,error}
frontend/src/components/ui/{Button,ThemeToggle}
frontend/src/{hooks/useTheme.ts,lib/theme.ts,lib/themeStore.ts}
frontend/tests/{theme,theme-toggle,design-tokens}
```

P4.3 (this milestone):

```text
frontend/src/app/page.tsx                    → redirect("/overview")
frontend/src/app/not-found.tsx
frontend/src/app/(dashboard)/layout.tsx      → <AppShell>
frontend/src/app/(dashboard)/overview/page.tsx
frontend/src/app/(dashboard)/employees/page.tsx
frontend/src/app/(dashboard)/attendance/page.tsx
frontend/src/app/(dashboard)/system-status/page.tsx
frontend/src/components/layout/{AppShell,Sidebar,Header,MobileNavigation,PagePlaceholder}
                                             + one CSS Module each
frontend/src/components/icons/NavIcon.tsx
frontend/src/lib/navigation.ts
frontend/tests/{navigation,app-shell,mobile-navigation,routes}
```

Removed: `src/app/layout.module.css` and `src/app/page.module.css` (the P4.2
header and landing styles, superseded by the shell), and the P4.2
`tests/home-page.test.tsx` (the landing page no longer exists). Its theme and
design-token suites are untouched and still pass.

Still absent by design: API client, Route Handler proxy, fixture modules, any
`NEXT_PUBLIC_` backend value.

---

## 4. Shell and navigation

| Piece | Responsibility |
|---|---|
| `AppShell` | layout; the only client state is the drawer's open flag |
| `Sidebar` | persistent desktop navigation (≥ 900 px), branded |
| `Header` | menu trigger (`aria-expanded`/`aria-controls`), current area label, theme toggle |
| `MobileNavigation` | modal drawer: scrim, close button, Escape, closes on link selection |
| `PagePlaceholder` | shared placeholder: title, purpose, planned work, "under development" badge |
| `navigation.ts` | single source of truth: `NAV_ITEMS`, `DEFAULT_ROUTE`, `isActivePath`, `findNavItem` |

Routes: `/` → `/overview`; `/overview`, `/employees`, `/attendance`,
`/system-status`; unmatched paths render `not-found`. All routes are static and
prerendered, directly addressable, and inside the `(dashboard)` group so the shell
is shared rather than duplicated.

Accessibility specifics: named `nav` landmarks ("Primary", "Mobile"),
`aria-current="page"` on the active link, skip link to `#main-content`, focus
moved into the drawer on open and restored to the trigger on explicit close and
Escape, drawer unmounted when closed, decorative SVG icons marked `aria-hidden`,
visible focus rings and reduced-motion support inherited from the tokens.

---

## 5. Verification (P4.3)

| Command | Result |
|---|---|
| `npm ls --depth=0` | exit 0 — 15 packages, unchanged |
| `npm run lint` | exit 0 — 0 problems |
| `npm run typecheck` | exit 0 — strict |
| `npm run test` | exit 0 — **85 passed / 85**, 7 suites, 21.9 s |
| `npm run build` | exit 0 — static `/`, `/_not-found`, `/attendance`, `/employees`, `/overview`, `/system-status` |

Test growth: 56 → 85 (+29). Two failures were found and fixed in implementation
rather than papered over: TypeScript rejected two `jest-dom` matchers that the
approved testing stack does not provide (replaced with plain DOM attribute
assertions), and the keyboard-path test initially assumed the wrong tab order
because jsdom does not apply the sidebar's media query — the test now asserts the
real document order. No rule was disabled, no test weakened, no dependency added.

---

## 6. Remaining work and constraints

* The four areas are structural placeholders — no metrics, tables, checkout
  controls or health checks until P4.4–P4.7.
* Live verification stays blocked until the owner authorizes a running FastAPI
  process and database (the G2+G8 startup gate refuses without PostgreSQL).
* Attendance has **no server-side pagination** and events carry **no employee
  name** — the attendance screen must paginate client-side and join names from
  `GET /employees`.
* Check-out accepts no request body and enforces the approved 300-second
  cooldown, which must be surfaced as-is.
* Responsive layout is implemented and unit-tested but has **not** been verified
  in a real browser at 360/768/1280 px.
* No authentication or RBAC until Phase 5; the backend stays loopback-only with
  CORS disabled.
